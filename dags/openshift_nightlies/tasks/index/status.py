import sys
from os.path import abspath, dirname
from os import environ

sys.path.insert(0, dirname(dirname(abspath(dirname(__file__)))))
from util import var_loader, kubeconfig, constants

import json
import requests

from airflow.operators.bash_operator import BashOperator
from airflow.models import Variable
from kubernetes.client import models as k8s

# Defines Task for Indexing Task Status in ElasticSearch
class StatusIndexer():
    def __init__(self, dag, version, release_stream, platform, profile):

        self.exec_config = {
            "pod_override": k8s.V1Pod(
                spec=k8s.V1PodSpec(
                    containers=[
                        k8s.V1Container(
                            name="base",
                            image="quay.io/keithwhitley4/airflow-ansible:2.0.0",
                            image_pull_policy="Always",
                            volume_mounts=[
                                kubeconfig.get_kubeconfig_volume_mount()]

                        )
                    ],
                    volumes=[kubeconfig.get_kubeconfig_volume(
                        version, platform, profile)]
                )
            )
        }

        # General DAG Configuration
        self.dag = dag
        self.platform = platform  # e.g. aws
        self.version = version  # e.g. 4.6/4.7, major.minor only
        self.release_stream = release_stream # true release stream to follow. Nightlies, CI, etc. 
        self.profile = profile  # e.g. default/ovn

        # Specific Task Configuration
        self.vars = var_loader.build_task_vars(
            task="index", version=version, platform=platform, profile=profile)

        self.release_stream_base_url = Variable.get("release_stream_base_url")
        latest_release = var_loader.get_latest_release_from_stream(self.release_stream_base_url, self.release_stream)

        self.env = {
            "OPENSHIFT_CLIENT_LOCATION": latest_release["openshift_client_location"],
            "RELEASE_STREAM": self.release_stream
        }


    # Create Airflow Task for Indexing Results into ElasticSearch
    def get_index_task(self):
        env = {
            **self.env, 
            **{"ES_SERVER": var_loader.get_elastic_url()},
            **environ
        }

        return BashOperator(
            task_id=f"index_results",
            depends_on_past=False,
            bash_command=f"{constants.root_dag_dir}/scripts/index.sh ",
            retries=3,
            dag=self.dag,
            trigger_rule="all_done",
            executor_config=self.exec_config,
            env=env
        )

