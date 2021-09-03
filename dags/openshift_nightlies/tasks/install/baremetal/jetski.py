from os import environ

from openshift_nightlies.util import var_loader, executor, constants
from openshift_nightlies.tasks.install.baremetal import webfuse
from openshift_nightlies.tasks.install.openshift import AbstractOpenshiftInstaller
from openshift_nightlies.tasks.benchmarks import e2e
from openshift_nightlies.models.release import BaremetalRelease
from openshift_nightlies.models.dag_config import DagConfig


import json

from airflow.operators.bash import BashOperator
from airflow.models import Variable
from airflow.utils.task_group import TaskGroup
from airflow.utils.helpers import chain

from kubernetes.client import models as k8s

# Defines Tasks for installation of Openshift Clusters
class BaremetalOpenshiftInstaller(AbstractOpenshiftInstaller):
    def __init__(self, dag, config: DagConfig, release: BaremetalRelease):
        self.baremetal_install_secrets = var_loader.get_secret(
            f"baremetal_openshift_install_config", deserialize_json=True)
        super().__init__(dag, config, release)

    def get_install_task(self):
        benchmarks = self._add_benchmarks(task_group="install-bench")
        install = self._get_task(operation="install")
        install >> benchmarks

        return install

    def get_scaleup_task(self):
        benchmarks = self._add_benchmarks(task_group="scaleup-bench")
        scaleup = self._get_task(operation="scaleup")
        scaleup >> benchmarks

        return scaleup
        
    def _add_benchmarks(self, task_group):
        with TaskGroup(task_group, prefix_group_id=True, dag=self.dag) as benchmarks:
            benchmark_tasks = self._get_e2e_benchmarks(task_group).get_benchmarks()
            chain(*benchmark_tasks)
        return benchmarks

    def _get_e2e_benchmarks(self, task_group):
        return e2e.E2EBenchmarks(self.dag, self.dag_config, self.release, task_group)
        
    # Create Airflow Task for Install/Cleanup steps
    def _get_task(self, operation="install", trigger_rule="all_success"):
        bash_script = ""

        # Merge all variables, prioritizing Airflow Secrets over git based vars
        config = {
            **self.vars,
            **self.baremetal_install_secrets,
            **{ "es_server": var_loader.get_secret('elasticsearch') }
        }
        
        config['pullsecret'] = json.dumps(config['openshift_install_pull_secret'])
        config['version'] = self.release.release_stream
        config['build'] = self.release.build
        
        # Required Environment Variables for Install script
        env = {
            "SSHKEY_TOKEN": config['sshkey_token'],
            "OPENSHIFT_WORKER_COUNT": config['openshift_worker_count'],
            "BAREMETAL_NETWORK_CIDR": config['baremetal_network_cidr'],
            "BAREMETAL_NETWORK_VLAN": config['baremetal_network_vlan'],
            "OPENSHIFT_BASE_DOMAIN": config['openshift_base_domain'],
            "JETSKI_SKIPTAGS": config['jetski_skiptags'],
            "KUBECONFIG_PATH": config['kubeconfig_path'],
            "KUBECONFIG_NAME": f"{self.release_name}-kubeconfig",
            "KUBEADMIN_NAME": f"{self.release_name}-kubeadmin",            
            **self._insert_kube_env()
        }

        if operation == "install":
            config['worker_count'] = config['openshift_worker_count']
            bash_script = f"{constants.root_dag_dir}/scripts/install/baremetal_install.sh"
        else:
            config['worker_count'] = config['openshift_worker_scaleup_target']
            bash_script = f"{constants.root_dag_dir}/scripts/install/baremetal_scaleup.sh"

        # Dump all vars to json file for Ansible to pick up
        with open(f"/tmp/{self.release_name}-{operation}-task.json", 'w') as json_file:
            json.dump(config, json_file, sort_keys=True, indent=4)

        return BashOperator(
            task_id=f"{operation}-cluster",
            depends_on_past=False,
            bash_command=f"{bash_script} -p {self.release.platform} -v {self.release.version} -j /tmp/{self.release_name}-{operation}-task.json -o {operation} ",
            retries=3,
            dag=self.dag,
            trigger_rule=trigger_rule,
            executor_config=self.exec_config,
            env=env
        )

    # This Helper Injects Airflow environment variables into the task execution runtime
    # This allows the task to interface with the Kubernetes cluster Airflow is hosted on.
    def _insert_kube_env(self):
        return {key: value for (key, value) in environ.items() if "KUBERNETES" in key}
