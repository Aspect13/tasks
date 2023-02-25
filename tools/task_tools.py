import logging
from uuid import uuid4
from werkzeug.utils import secure_filename
from urllib.parse import urlunparse, urlparse
import requests
from datetime import datetime
from io import BytesIO
from flask import current_app
from sqlalchemy import and_
from arbiter import Arbiter

from ..models.tasks import Task
from ..models.results import TaskResults
from tools import constants as c, api_tools, rpc_tools, data_tools, secrets_tools, MinioClient
from pylon.core.tools import log


def get_arbiter():
    return Arbiter(host=c.RABBIT_HOST, port=c.RABBIT_PORT, user=c.RABBIT_USER, password=c.RABBIT_PASSWORD)


def create_task(project, file, args):
    if isinstance(file, str):
        file = data_tools.files.File(file)
    filename = str(uuid4())
    filename = secure_filename(filename)
    api_tools.upload_file(bucket="tasks", f=file, project=project)
    task = Task(
        task_id=filename,
        project_id=project.id,
        zippath=f"tasks/{file.filename}",
        task_name=args.get("funcname"),
        task_handler=args.get("invoke_func"),
        runtime=args.get("runtime"),
        region=args.get("region"),
        env_vars=args.get("env_vars")
    )
    task.insert()
    return task


def check_task_quota(task, project_id=None, quota='tasks_executions'):
    # TODO: we need to calculate it based on VUH, if we haven't used VUH quota then run
    return {"message", "ok"}


def run_task(project_id, event, task_id=None, queue_name=None) -> dict:
    if not queue_name:
        queue_name = c.RABBIT_QUEUE_NAME
    secrets = secrets_tools.get_project_hidden_secrets(project_id=project_id)
    secrets.update(secrets_tools.get_project_secrets(project_id=project_id))
    task_id = task_id if task_id else secrets["control_tower_id"]
    task = Task.query.filter(and_(Task.task_id == task_id)).first()
    check_task_quota(task)
    arbiter = get_arbiter()
    task_kwargs = {
        "task": secrets_tools.unsecret(value=task.to_json(), secrets=secrets, project_id=project_id),
        "event": secrets_tools.unsecret(value=event, secrets=secrets, project_id=project_id),
        "galloper_url": secrets_tools.unsecret(
            value="{{secret.galloper_url}}",
            secrets=secrets,
            project_id=task.project_id
        ),
        "token": secrets_tools.unsecret(value="{{secret.auth_token}}", secrets=secrets, project_id=task.project_id)
    }
    arbiter.apply("execute_lambda", queue=queue_name, task_kwargs=task_kwargs)
    arbiter.close()
    rpc_tools.RpcMixin().rpc.call.projects_add_task_execution(project_id=task.project_id)
    return {"message": "Accepted", "code": 200, "task_id": task_id}


def create_task_result(project_id: int,  data: dict):
    task_result = TaskResults(
        project_id=project_id,
        task_id=data.get('task_id'),
        ts=data.get('ts'),
        results=data.get('results'),
        log=data.get('log'),
        task_duration=data.get('task_duration'),
        task_status=data.get('task_status'),
        task_result_id=data.get('task_result_id'),
        task_stats=data.get('task_stats'),
    )
    task_result.insert()
    return task_result


def write_task_run_logs_to_minio_bucket(project_id, task, project):
    loki_settings_url = urlparse(current_app.config["CONTEXT"].settings.get('loki', {}).get('url'))
    task_result_id = task.task_result_id
    task_name = task.task_name
    task_id = task.task_id

    if loki_settings_url:
        logs_query = "{" + f'hostname="{task_name}", task_id="{task_id}",project="{project_id}", task_result_id="{task_result_id}"' + "}"

        loki_url = urlunparse((
            loki_settings_url.scheme,
            loki_settings_url.netloc,
            '/loki/api/v1/query_range',
            None,
            'query=' + logs_query,
            None
        ))
        response = requests.get(loki_url)

        if response.ok:
            results = response.json()
            log.info(results)
            enc = 'utf-8'
            file_output = BytesIO()

            file_output.write(f'Task {task.task_name} (task_result_id={task.task_result_id}) run log:\n'.encode(enc))

            unpacked_values = []
            for i in results['data']['result']:
                for ii in i['values']:
                    unpacked_values.append(ii)
            for unix_ns, log_line in sorted(unpacked_values, key=lambda x: int(x[0])):
                timestamp = datetime.fromtimestamp(int(unix_ns) / 1e9).strftime("%Y-%m-%d %H:%M:%S")
                file_output.write(
                    f'{timestamp}\t{log_line}\n'.encode(enc)
                )
            minio_client = MinioClient(project)
            file_output.seek(0)
            bucket_name = str(task.task_name).replace("_", "").replace(" ", "").lower()
            file_name = f"{task_result_id}.log"
            if bucket_name not in minio_client.list_bucket():
                minio_client.create_bucket(bucket=bucket_name, bucket_type='autogenerated')
            minio_client.upload_file(bucket_name, file_output, file_name)
        else:
            log.warning('Request to loki failed with status %s', response.status_code)
