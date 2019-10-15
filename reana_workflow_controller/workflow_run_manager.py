# This file is part of REANA.
# Copyright (C) 2018, 2019 CERN.
#
# REANA is free software; you can redistribute it and/or modify it
# under the terms of the MIT License; see LICENSE file for more details.

"""Workflow run manager interface."""
import base64
import json
import os
from string import Template

import pkg_resources
import yaml
from kubernetes import client
from kubernetes.client.models.v1_delete_options import V1DeleteOptions
from kubernetes.client.rest import ApiException
from reana_commons.config import CVMFS_REPOSITORIES, INTERACTIVE_SESSION_TYPES
from reana_commons.k8s.api_client import (current_k8s_batchv1_api_client,
                                          current_k8s_corev1_api_client,
                                          current_k8s_extensions_v1beta1)
from reana_commons.utils import (create_cvmfs_persistent_volume_claim,
                                 create_cvmfs_storage_class)
from reana_db.database import Session

from reana_workflow_controller.errors import REANAInteractiveSessionError
from reana_workflow_controller.k8s import (build_interactive_k8s_objects,
                                           delete_k8s_ingress_object,
                                           delete_k8s_objects_if_exist,
                                           instantiate_chained_k8s_objects)

from reana_workflow_controller.config import (  # isort:skip
    MANILA_CEPHFS_PVC,
    REANA_STORAGE_BACKEND,
    REANA_WORKFLOW_ENGINE_IMAGE_CWL,
    REANA_WORKFLOW_ENGINE_IMAGE_SERIAL,
    REANA_WORKFLOW_ENGINE_IMAGE_YADAGE,
    SHARED_FS_MAPPING,
    TTL_SECONDS_AFTER_FINISHED,
    WORKFLOW_ENGINE_COMMON_ENV_VARS,
    WORKFLOW_ENGINE_COMMON_ENV_VARS_DEBUG)


class WorkflowRunManager():
    """Interface which specifies how to manage workflow runs."""

    if os.getenv('FLASK_ENV') == 'development':
        WORKFLOW_ENGINE_COMMON_ENV_VARS.extend(
            WORKFLOW_ENGINE_COMMON_ENV_VARS_DEBUG)

    engine_mapping = {
        'cwl': {'image': '{}'.
                         format(REANA_WORKFLOW_ENGINE_IMAGE_CWL),
                'command': ("run-cwl-workflow "
                            "--workflow-uuid {id} "
                            "--workflow-workspace {workspace} "
                            "--workflow-json '{workflow_json}' "
                            "--workflow-parameters '{parameters}' "
                            "--operational-options '{options}' "),
                'environment_variables': WORKFLOW_ENGINE_COMMON_ENV_VARS},
        'yadage': {'image': '{}'.
                            format(REANA_WORKFLOW_ENGINE_IMAGE_YADAGE),
                   'command': ("run-yadage-workflow "
                               "--workflow-uuid {id} "
                               "--workflow-workspace {workspace} "
                               "--workflow-json '{workflow_json}' "
                               "--workflow-parameters '{parameters}' "),
                   'environment_variables': WORKFLOW_ENGINE_COMMON_ENV_VARS},
        'serial': {'image': '{}'.
                            format(REANA_WORKFLOW_ENGINE_IMAGE_SERIAL),
                   'command': ("run-serial-workflow "
                               "--workflow-uuid {id} "
                               "--workflow-workspace {workspace} "
                               "--workflow-json '{workflow_json}' "
                               "--workflow-parameters '{parameters}' "
                               "--operational-options '{options}' "),
                   'environment_variables': WORKFLOW_ENGINE_COMMON_ENV_VARS},
    }
    """Mapping between engines and their basis configuration."""

    def __init__(self, workflow):
        """Initialise a WorkflowRunManager.

        :param workflow: An instance of :class:`reana_db.models.Workflow`.
        """
        self.workflow = workflow

    def _workflow_run_name_generator(self, mode, type=None):
        """Generate the name to be given to a workflow run.

        In the case of Kubernetes, this should allow administrators to be able
        to easily find workflow runs i.e.:
        .. code-block:: console

           $ kubectl get pods | grep batch
           batch-serial-64594f48ff-5mgbz  0/1  Running 0   1m
           batch-cwl-857bb969bb-john-64f97d955d-jklcw  0/1  Running 0  16h

           $ kubectl get pods | grep interactive
           interactive-yadage-7fbb558577-xdxn8  0/1  Running  0  30m

        :param mode: Mode in which the workflow runs, like ``batch`` or
            ``interactive``.
        """
        type_ = type or self.workflow.type_
        return '{mode}-{workflow_type}-{workflow_id}'.format(
            mode=mode,
            workflow_id=self.workflow.id_,
            workflow_type=type_,
        )

    def _generate_interactive_workflow_path(self):
        """Generate the path to access the interactive workflow."""
        return "/{}".format(self.workflow.id_)

    def _get_merged_workflow_parameters(self):
        """Return workflow input parameters merged with live ones, if given."""
        if self.workflow.input_parameters:
            return dict(self.workflow.get_input_parameters(),
                        **self.workflow.input_parameters)
        else:
            return self.workflow.get_input_parameters()

    def start_batch_workflow_run(self):
        """Start a batch workflow run."""
        raise NotImplementedError('')

    def start_interactive_session(self):
        """Start an interactive workflow run."""
        raise NotImplementedError('')

    def stop_batch_workflow_run(self):
        """Stop a batch workflow run."""
        raise NotImplementedError('')

    def _workflow_engine_image(self):
        """Return the correct image for the current workflow type."""
        return WorkflowRunManager.engine_mapping[self.workflow.type_]['image']

    def _workflow_engine_command(self):
        """Return the command to be run for a given workflow engine."""
        return (WorkflowRunManager.engine_mapping[self.workflow.type_]
                ['command'].format(
                    id=self.workflow.id_,
                    workspace=self.workflow.get_workspace(),
                    workflow_json=base64.standard_b64encode(json.dumps(
                        self.workflow.get_specification()).encode()),
                    parameters=base64.standard_b64encode(json.dumps(
                        self._get_merged_workflow_parameters()).encode()),
                    options=base64.standard_b64encode(json.dumps(
                        self.workflow.operational_options).encode())))

    def _workflow_engine_env_vars(self):
        """Return necessary environment variables for the workflow engine."""
        env_vars = list(WorkflowRunManager.engine_mapping[
            self.workflow.type_]['environment_variables'])
        cvmfs_volumes = 'false'
        for resource, value in self.workflow.reana_specification['workflow'].\
                get('resources', {}).items():
            if 'cvmfs' in resource:
                cvmfs_volumes = value
                break
        if type(cvmfs_volumes) == list:
            cvmfs_env_var = {'name': 'REANA_MOUNT_CVMFS',
                             'value': str(cvmfs_volumes)}
            env_vars.append(cvmfs_env_var)
            for cvmfs_volume in cvmfs_volumes:
                if cvmfs_volume in CVMFS_REPOSITORIES:
                    create_cvmfs_storage_class(cvmfs_volume)
                    create_cvmfs_persistent_volume_claim(cvmfs_volume)

        return (env_vars)


class KubernetesWorkflowRunManager(WorkflowRunManager):
    """Implementation of WorkflowRunManager for Kubernetes."""

    k8s_shared_volume = {
        'cephfs': {
            'name': 'default-shared-volume',
            'persistentVolumeClaim': {
                'claimName': MANILA_CEPHFS_PVC,
                'readOnly': False,
            }
        },
        'local': {
            'name': 'default-shared-volume',
            'hostPath': {
                'path': SHARED_FS_MAPPING['MOUNT_SOURCE_PATH'],
                'type': 'DirectoryOrCreate',
            }
        }
    }

    default_namespace = 'default'
    """Default Kubernetes namespace."""

    def start_batch_workflow_run(self):
        """Start a batch workflow run."""
        workflow_run_name = self._workflow_run_name_generator('batch')
        job = self._create_job_spec(workflow_run_name)
        current_k8s_batchv1_api_client.create_namespaced_job(
            namespace=KubernetesWorkflowRunManager.default_namespace,
            body=job)

    def start_interactive_session(self, interactive_session_type, **kwargs):
        """Start an interactive workflow run.

        :param interactive_session_type: One of the available interactive
            session types.
        :return: Relative path to access the interactive session.
        """
        action_completed = True
        try:
            if (interactive_session_type
                    not in INTERACTIVE_SESSION_TYPES):
                raise REANAInteractiveSessionError(
                    "Interactive type {} does not exist.".format(
                        interactive_session_type))
            access_path = self._generate_interactive_workflow_path()
            self.workflow.interactive_session_type = interactive_session_type
            self.workflow.interactive_session = access_path
            workflow_run_name = \
                self._workflow_run_name_generator(
                    'interactive', type=interactive_session_type)
            self.workflow.interactive_session_name = workflow_run_name
            kubernetes_objects = \
                build_interactive_k8s_objects[interactive_session_type](
                    workflow_run_name, self.workflow.get_workspace(),
                    access_path,
                    access_token=self.workflow.get_owner_access_token(),
                    **kwargs)

            instantiate_chained_k8s_objects(
                kubernetes_objects,
                KubernetesWorkflowRunManager.default_namespace)
            return access_path

        except KeyError:
            action_completed = False
            raise REANAInteractiveSessionError(
                "Unsupported interactive session type {}.".format(
                    interactive_session_type
                ))
        except ApiException as api_exception:
            action_completed = False
            raise REANAInteractiveSessionError(
                "Connection to Kubernetes has failed:\n{}".format(
                    api_exception)
            )
        except Exception as e:
            action_completed = False
            raise REANAInteractiveSessionError(
                "Unkown error while starting interactive workflow run:\n{}"
                .format(e)
            )
        finally:
            if not action_completed:
                self.workflow.interactive_session = None
                if kubernetes_objects:
                    delete_k8s_objects_if_exist(
                        kubernetes_objects,
                        KubernetesWorkflowRunManager.default_namespace)

            current_db_sessions = Session.object_session(self.workflow)
            current_db_sessions.add(self.workflow)
            current_db_sessions.commit()

    def stop_interactive_session(self):
        """Stop an interactive workflow run."""
        delete_k8s_ingress_object(
            ingress_name=self.workflow.interactive_session_name,
            namespace=KubernetesWorkflowRunManager.default_namespace
        )
        self.workflow.interactive_session_name = None
        self.workflow.interactive_session = None
        current_db_sessions = Session.object_session(self.workflow)
        current_db_sessions.add(self.workflow)
        current_db_sessions.commit()

    def stop_batch_workflow_run(self, workflow_run_jobs=None):
        """Stop a batch workflow run along with all its dependent jobs.

        :param workflow_run_jobs: List of active job id's spawned by the
            workflow run.
        """
        workflow_run_name = self._workflow_run_name_generator('batch')
        workflow_run_jobs = workflow_run_jobs or []
        to_delete = workflow_run_jobs + [workflow_run_name]
        for job in to_delete:
            current_k8s_batchv1_api_client.delete_namespaced_job(
                job,
                KubernetesWorkflowRunManager.default_namespace,
                V1DeleteOptions(propagation_policy='Background'))

    def _create_job_spec(self, name, command=None, image=None,
                         env_vars=None):
        """Instantiate a Kubernetes job.

        :param name: Name of the job.
        :param image: Docker image to use to run the job on.
        :param command: List of commands to run on the given job.
        :param env_vars: List of environment variables (dictionaries) to
            inject into the workflow engine container.
        """
        image = image or self._workflow_engine_image()
        command = command or self._workflow_engine_command()
        env_vars = env_vars or self._workflow_engine_env_vars()
        if isinstance(command, str):
            command = [command]
        elif not isinstance(command, list):
            raise ValueError('Command should be a list or a string and not {}'
                             .format(type(command)))

        workflow_metadata = client.V1ObjectMeta(name=name)
        job = client.V1Job()
        job.api_version = 'batch/v1'
        job.kind = 'Job'
        job.metadata = workflow_metadata
        spec = client.V1JobSpec(
            template=client.V1PodTemplateSpec())
        spec.template.metadata = workflow_metadata
        container = client.V1Container(name=name, image=image,
                                       image_pull_policy='IfNotPresent',
                                       env=[], volume_mounts=[],
                                       command=['/bin/bash', '-c'],
                                       args=command)
        container.env.extend(env_vars)
        container.volume_mounts = [
            {
                'name': 'default-shared-volume',
                'mountPath': SHARED_FS_MAPPING['MOUNT_DEST_PATH'],
            },
        ]
        spec.template.spec = client.V1PodSpec(containers=[container])
        spec.template.spec.volumes = [
            KubernetesWorkflowRunManager.k8s_shared_volume
            [REANA_STORAGE_BACKEND]
        ]
        job.spec = spec
        job.spec.template.spec.restart_policy = 'Never'
        job.spec.ttl_seconds_after_finished = TTL_SECONDS_AFTER_FINISHED
        job.spec.backoff_limit = 0
        return job
