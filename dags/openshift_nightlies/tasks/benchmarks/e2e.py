import sys
from os.path import abspath, dirname
from os import environ

sys.path.insert(0, dirname(dirname(abspath(dirname(__file__)))))
from util import var_loader, kubeconfig, constants
from tasks.index.status import StatusIndexer
from models.release import OpenshiftRelease

import json
from datetime import timedelta
from airflow.operators.bash_operator import BashOperator
from airflow.operators.subdag_operator import SubDagOperator
from airflow.models import Variable
from airflow.models import DAG
from airflow.utils.task_group import TaskGroup
from kubernetes.client import models as k8s




class E2EBenchmarks():
    def __init__(self, dag, release: OpenshiftRelease, task_group="benchmarks"):
        # General DAG Configuration
        self.dag = dag
        self.release = release
        self.task_group = task_group
        self.exec_config = var_loader.get_executor_config_with_cluster_access(self.release)

        

        # Airflow Variables
        self.SNAPPY_DATA_SERVER_URL = Variable.get("SNAPPY_DATA_SERVER_URL")
        self.SNAPPY_DATA_SERVER_USERNAME = Variable.get("SNAPPY_DATA_SERVER_USERNAME")
        self.SNAPPY_DATA_SERVER_PASSWORD = Variable.get("SNAPPY_DATA_SERVER_PASSWORD")

        # Specific Task Configuration
        self.vars = var_loader.build_task_vars(
            release=self.release, task="benchmarks")
        print("specific task configuration: ", self.vars, "release: ", self.release)
        self.git_name=self._git_name()
        self.env = {
            "SNAPPY_DATA_SERVER_URL": self.SNAPPY_DATA_SERVER_URL,
            "SNAPPY_DATA_SERVER_USERNAME": self.SNAPPY_DATA_SERVER_USERNAME,
            "SNAPPY_DATA_SERVER_PASSWORD": self.SNAPPY_DATA_SERVER_PASSWORD,
            "SNAPPY_USER_FOLDER": self.git_name,
            "PLATFORM": self.release.platform,
            "TASK_GROUP": self.task_group
        }

        if self.release.platform == "baremetal":
            self.install_vars = var_loader.build_task_vars(
                release, task="install")
            self.baremetal_install_secrets = Variable.get(
            f"baremetal_openshift_install_config", deserialize_json=True)

            self.config = {
                **self.install_vars,
                **self.baremetal_install_secrets
            }

            self.env = {
                **self.env,
                "SSHKEY_TOKEN": self.config['sshkey_token'],
                "ORCHESTRATION_USER": self.config['provisioner_user'],
                "ORCHESTRATION_HOST": self.config['provisioner_hostname']
            }
    

    def get_benchmarks(self):
        benchmarks = self._get_benchmarks(self.vars["benchmarks"])
        with TaskGroup("Index Results", prefix_group_id=False, dag=self.dag) as post_steps:
            indexers = self._add_indexers(benchmarks)
        return benchmarks

    def _git_name(self):
        git_username = var_loader.get_git_user()
        if git_username == 'cloud-bulldozer':
            return f"perf-ci"
        else: 
            return f"{git_username}"

    def _get_benchmarks(self, benchmarks):
        for index, benchmark in enumerate(benchmarks):
            if 'benchmarks' not in benchmark:
                benchmarks[index] = self._get_benchmark(benchmark)
            elif 'group' in benchmark:
                with TaskGroup(benchmark['group'], prefix_group_id=False, dag=self.dag) as task_group:
                    benchmarks[index] = self._get_benchmarks(benchmark['benchmarks'])
            else: 
                benchmarks[index] = self._get_benchmarks(benchmark['benchmarks'])
        return benchmarks

    def _add_indexers(self, benchmarks):
            for index, benchmark in enumerate(benchmarks):
                if isinstance(benchmark, BashOperator):
                    self._add_indexer(benchmark)
                elif isinstance(benchmark, list):
                    self._add_indexers(benchmark)

    def _add_indexer(self, benchmark): 
        indexer = StatusIndexer(self.dag, self.release, benchmark.task_id).get_index_task() 
        benchmark >> indexer 

    def _get_benchmark(self, benchmark):
        #var = self.vars
        #print("var dict: ", var)
        # print ("NODE_COUNT: ", NODE_COUNT)
        #print ("_get_benchmark vars: ", var)
        #print ("type of vars: ", type(var))
        #print ("type of env: ", type(self.env))
        env = {**self.env, **benchmark.get('env', {}), **{"ES_SERVER": var_loader.get_elastic_url()}, **{"KUBEADMIN_PASSWORD": environ.get("KUBEADMIN_PASSWORD", "")}}
        #print("Environment: ", env)
        #with open('/environment.txt', 'w') as f:
        #        print(env, file=f)
        return BashOperator(
            task_id=f"{self.task_group}_{benchmark['name']}",
            depends_on_past=False,
            bash_command=f"{constants.root_dag_dir}/scripts/run_benchmark.sh -w {benchmark['workload']} -c {benchmark['command']} ",
            retries=3,
            dag=self.dag,
            env=env,
            executor_config=self.exec_config
        )
