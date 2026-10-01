"""Infrastructure-as-code nodes (verinoda/infra.py): Dockerfiles, compose services, Kubernetes containers and
Terraform resources read as text, each container linked to the project file its command runs, with the manifest
and COPY lines as evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verinoda import cli, infra

FILES = {
    "app/__init__.py": "",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n\n\nif __name__ == \"__main__\":\n    run()\n",
    "worker/tasks.py": "def work():\n    pass\n",
    "requirements.txt": "fastapi\n",
    "Dockerfile": """# the API image
FROM python:3.12-slim AS base
WORKDIR /srv
COPY requirements.txt .
COPY app/ ./app/
CMD ["uvicorn", "app.main:app", \\
     "--host", "0.0.0.0"]
""",
    "web/server.js": "require('http').createServer().listen(3000);\n",
    "web/Dockerfile": "FROM node:20\nWORKDIR /usr/src/app\nCOPY . .\nCMD node server.js\n",
    "svc/pom.xml": "<project/>\n",
    "svc/src/main/java/com/x/Main.java": "package com.x;\nclass Main {}\n",
    "svc/Dockerfile": """FROM maven:3 AS build
WORKDIR /src
COPY . .
RUN mvn package
FROM eclipse-temurin:21
COPY --from=build /src/target/svc.jar /app/svc.jar
ENTRYPOINT ["java", "-jar", "/app/svc.jar"]
""",
    "docker-compose.yml": """services:
  api:
    build: .
    image: acme/api:1.0   # the API
    command: python -m app.main
  worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: ["celery", "-A", "worker.tasks", "worker"]
  db:
    image: postgres:16
""",
    "k8s/deploy.yaml": """apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
        - name: api
          image: registry.local/acme/api:1.0
          args:
            - uvicorn
            - app.main:app
        - name: proxy
          image: nginx:1.25
---
apiVersion: v1
kind: Service
metadata:
  name: api
""",
    "lambda/handler.py": "def run(event, context):\n    return 1\n",
    "infra/main.tf": """resource "aws_lambda_function" "fn" {
  function_name = "fn"
  handler       = "handler.run"
  filename      = "${path.module}/build.zip"
}

data "archive_file" "zip" {
  type        = "zip"
  source_dir  = "${path.module}/../lambda"
  output_path = "build.zip"
}
""",
}


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel, text in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


def _node(res: dict, kind: str, name: str) -> dict:
    return next(n for n in res["nodes"] if n["kind"] == kind and n["name"] == name)


def _link(node: dict, i: int = 0) -> dict:
    return node["runs"][i]["link"]


def test_a_dockerfile_entry_point_is_linked_through_its_copy_line(repo):
    res = infra.run(repo)
    node = _node(res, "dockerfile", "Dockerfile")
    assert node["status"] == "statically_verified"
    assert node["command"] == ["uvicorn", "app.main:app", "--host", "0.0.0.0"]
    assert node["command_at"] == ["Dockerfile:6"]           # continuation lines joined, the first line cited
    link = _link(node)
    assert (link["file"], link["line"], link["status"]) == ("app/main.py", 3, "strong_inference")  # app = ...
    assert [e["at"] for e in link["evidence"]] == ["Dockerfile:6", "Dockerfile:5"]
    assert link["evidence"][1]["text"] == "COPY app/ ./app/"


def test_node_and_multi_stage_java_images(repo):
    res = infra.run(repo)
    web = _link(_node(res, "dockerfile", "web/Dockerfile"))
    assert (web["file"], web["status"]) == ("web/server.js", "strong_inference")
    java = _node(res, "dockerfile", "svc/Dockerfile")
    run = java["runs"][0]
    assert (run["kind"], run["ref"]) == ("path", "/app/svc.jar")
    assert "svc/target/svc.jar is a build output" in run["why"]
    assert (run["link"]["file"], run["link"]["status"]) == ("svc/pom.xml", "weak_inference")
    assert [e["at"] for e in run["link"]["evidence"]] == ["svc/Dockerfile:7", "svc/Dockerfile:6", "svc/Dockerfile:3"]


def test_compose_services_override_the_dockerfile_command(repo):
    res = infra.run(repo)
    api = _node(res, "compose_service", "api")
    assert api["at"] == "docker-compose.yml:2" and api["image"] == "acme/api:1.0"
    assert api["build"]["dockerfile"] == "Dockerfile"
    assert api["command"] == ["python", "-m", "app.main"] and api["command_at"] == ["docker-compose.yml:5"]
    link = _link(api)
    assert (link["file"], link["line"], link["status"]) == ("app/main.py", 6, "strong_inference")  # __main__ guard
    # worker/ is never copied into the image: found by its path's ending only
    worker = _link(_node(res, "compose_service", "worker"))
    assert (worker["file"], worker["status"]) == ("worker/tasks.py", "weak_inference")
    assert "not through COPY" in worker["note"]
    db = _node(res, "compose_service", "db")
    assert db["runs"] == [] and "no command" in db["why"]


def test_kubernetes_containers_use_the_image_built_by_the_project(repo):
    res = infra.run(repo)
    api = _node(res, "k8s_container", "Deployment/api/api")
    assert api["at"] == "k8s/deploy.yaml:9"
    assert api["image_built_by"] == {"dockerfile": "Dockerfile", "status": "weak_inference",
                                     "note": "matched by the image's name"}
    assert api["command"] == ["uvicorn", "app.main:app"] and api["command_at"] == ["k8s/deploy.yaml:11"]
    link = _link(api)
    assert (link["file"], link["status"]) == ("app/main.py", "weak_inference")
    proxy = _node(res, "k8s_container", "Deployment/api/proxy")
    assert proxy["runs"] == [] and proxy["image"] == "nginx:1.25"
    assert not any(n["name"].startswith("Service/") for n in res["nodes"])


def test_terraform_resources_and_the_paths_they_name(repo):
    res = infra.run(repo)
    fn = _node(res, "terraform_resource", "aws_lambda_function.fn")
    assert fn["at"] == "infra/main.tf:1"
    handler = _link(fn)
    assert (handler["file"], handler["line"], handler["status"]) == ("lambda/handler.py", 1, "weak_inference")
    zipped = _node(res, "terraform_data", "archive_file.zip")
    link = _link(zipped)
    assert (link["file"], link["status"]) == ("lambda/", "strong_inference")
    assert link["evidence"][0]["at"] == "infra/main.tf:9"
    assert res["files_read"] == {"dockerfile": 3, "compose": 1, "kubernetes": 1, "terraform": 1}


def test_terraform_container_command_lists_are_resolved(tmp_path):
    (tmp_path / "jobs").mkdir()
    (tmp_path / "jobs" / "nightly.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "ecs.tf").write_text('resource "kubernetes_job" "nightly" {\n  spec {\n    container {\n'
                                     '      command = ["python", "jobs/nightly.py"]\n    }\n  }\n}\n',
                                     encoding="utf-8")
    node = infra.run(tmp_path)["nodes"][0]
    assert node["command"] == ["python", "jobs/nightly.py"] and node["command_at"] == ["ecs.tf:4"]
    assert _link(node)["file"] == "jobs/nightly.py"


@pytest.mark.parametrize("argv, want", [
    (["python3", "-u", "-m", "pkg.cli"], [("module", "pkg.cli")]),
    (["python", "manage.py", "runserver"], [("path", "manage.py")]),
    (["gunicorn", "-w", "4", "-b", "0.0.0.0:8000", "proj.wsgi:application"], [("module", "proj.wsgi")]),
    (["/bin/sh", "-c", "alembic upgrade head && exec uvicorn app.main:app"],
     [("program", "alembic"), ("module", "app.main")]),
    (["tini", "--", "node", "--enable-source-maps", "dist/index.js"], [("path", "dist/index.js")]),
    (["java", "-cp", "lib/*", "com.x.Main"], [("class", "com.x.Main")]),
    (["./docker-entrypoint.sh", "python", "run.py"], [("path", "./docker-entrypoint.sh"), ("path", "run.py")]),
    (["deno", "run", "--allow-net", "main.ts"], [("path", "main.ts")]),
    (["nginx", "-g", "daemon off;"], [("program", "nginx")]),
])
def test_what_a_command_runs(argv, want):
    assert infra.runs(argv) == want


def test_an_entrypoint_clears_the_cmd_of_the_stage_it_builds_on():
    df = infra.parse_dockerfile("Dockerfile", 'FROM python AS a\nCMD ["python", "a.py"]\nFROM a\n'
                                              'ENTRYPOINT ["python", "b.py"]\n')
    assert df.stages[1].cmd is None and df.stages[1].entrypoint[0] == ["python", "b.py"]
    df = infra.parse_dockerfile("Dockerfile", 'FROM python\nENTRYPOINT ["python"]\nCMD ["c.py"]\n')
    assert df.stages[0].cmd[0] == ["c.py"]


def test_a_file_copied_into_a_folder_does_not_stand_for_its_siblings(tmp_path):
    (tmp_path / "run.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python\nCOPY run.py /app/\nWORKDIR /app\nCMD python other.py\n",
                                         encoding="utf-8")
    run = infra.run(tmp_path)["nodes"][0]["runs"][0]
    assert run["link"] is None and run["why"] == "no project file found for it"


def test_the_yaml_reader_keeps_lines():
    doc = infra.yaml_docs("a:\n  b: [x, 'y z', \"w\"]  # c\n  c: >\n    one\n    two\n  d:\n  - k: 1\n    l: 2\n"
                          "  - plain\n")[0]
    assert [i.v for i in doc.get("a").get("b").v] == ["x", "y z", "w"]
    assert doc.get("a").get("b").line == 2
    assert doc.get("a").get("c").v == "one two"
    items = doc.get("a").get("d").v
    assert items[0].get("l").v == "2" and items[0].get("l").line == 8 and items[1].v == "plain"


def test_cli_json_file_filter_and_exit_codes(repo, tmp_path, capsys):
    assert cli.main(["infra", "--repo", str(repo), "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["status"] == "found" and res["linked"] >= 6
    assert cli.main(["infra", "--repo", str(repo), "--file", "app/main.py", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert {n["name"] for n in res["nodes"]} == {"Dockerfile", "api", "Deployment/api/api"}
    assert cli.main(["infra", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "-> app/main.py:3  [strong_inference]  module app.main  via Dockerfile:6, Dockerfile:5" in out
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["infra", "--repo", str(empty)]) == 1
    assert "no Dockerfile" in capsys.readouterr().out


def test_in_a_path_with_a_space_and_non_ascii(tmp_path):
    root = tmp_path / "proje ğüş"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("app = 1\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python\nCOPY . /code\nWORKDIR /code\nCMD [\"python\", \"app/main.py\"]\n",
                                     encoding="utf-8")
    link = _link(infra.run(root)["nodes"][0])
    assert (link["file"], link["status"]) == ("app/main.py", "strong_inference")


def _project(root: Path, files: dict[str, str], bom: bool = False) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(("﻿" if bom else "").encode("utf-8") + text.encode("utf-8"))
    return root


def test_a_compose_image_matched_by_name_is_weak(tmp_path):
    _project(tmp_path, {"api/main.py": "app = 1\n",
                        "api/Dockerfile": 'FROM python\nWORKDIR /srv\nCOPY . .\nCMD ["uvicorn", "main:app"]\n',
                        "deploy/docker-compose.yml": "services:\n  api:\n    image: registry.example.com/team/api:2.0\n"})
    node = _node(infra.run(tmp_path), "compose_service", "api")
    assert node["image_built_by"] == {"dockerfile": "api/Dockerfile", "status": "weak_inference",
                                      "note": "matched by the image's name"}
    link = _link(node)
    assert (link["file"], link["status"]) == ("api/main.py", "weak_inference")
    assert "matched by the image's name" in link["note"]


def test_terraform_names_and_non_code_paths_are_not_runs(tmp_path):
    _project(tmp_path, {"api/main.py": "x = 1\n", "web/index.js": "1\n", "README.md": "# r\n",
                        "main.tf": 'resource "aws_ecr_repository" "api" {\n  name = "api"\n  tags = {\n'
                                   '    Name = "web"\n  }\n}\n'
                                   'resource "local_file" "notes" {\n  filename = "README.md"\n}\n'})
    res = infra.run(tmp_path)
    assert _node(res, "terraform_resource", "aws_ecr_repository.api")["runs"] == []
    notes = _node(res, "terraform_resource", "local_file.notes")["runs"][0]
    assert notes["relation"] == "names" and notes["link"]["status"] == "weak_inference"
    assert notes["link"]["file"] == "README.md"


def test_terraform_comments_do_not_open_a_block(tmp_path):
    _project(tmp_path, {"src/a.py": "x = 1\n", "main.tf": (
        'resource "local_file" "notes" {\n  content = "x"\n}\n'
        'resource "aws_s3_object" "o" { // see {docs\n  source = "src/a.py" /* { */\n}\n'
        '/* resource "null_resource" "hidden" {\n*/\n'
        'resource "null_resource" "after" {\n  triggers = { a = "b" }\n}\n')})
    names = [n["name"] for n in infra.run(tmp_path)["nodes"]]
    assert names == ["local_file.notes", "aws_s3_object.o", "null_resource.after"]


def test_an_overlay_folder_copy_does_not_hide_the_copy_that_brought_the_file(tmp_path):
    _project(tmp_path, {"app.py": "x = 1\n", "config/settings.py": "y = 1\n", "tools/app.py": "z = 1\n",
                        "Dockerfile": 'FROM python\nWORKDIR /app\nCOPY . .\nCOPY config/ ./\nCMD ["python", "app.py"]\n'})
    link = _link(infra.run(tmp_path)["nodes"][0])
    assert (link["file"], link["status"]) == ("app.py", "strong_inference")
    assert [e["at"] for e in link["evidence"]] == ["Dockerfile:5", "Dockerfile:3"]


@pytest.mark.parametrize("copy, cmd, want", [
    ("COPY Procfile app.py ./", '["python", "app.py"]', "app.py"),           # a file with no extension first
    ("COPY my.pkg/ /srv/", '["python", "/srv/main.py"]', "my.pkg/main.py"),  # a folder with a dot
    ("COPY scripts/start /usr/local/bin/", '["/usr/local/bin/start"]', "scripts/start"),
])
def test_copy_sources_are_files_or_folders_as_the_project_has_them(tmp_path, copy, cmd, want):
    _project(tmp_path, {"app.py": "x = 1\n", "Procfile": "web: x\n", "my.pkg/main.py": "x = 1\n",
                        "scripts/start": "#!/bin/sh\n",
                        "Dockerfile": f"FROM python\nWORKDIR /app\n{copy}\nCMD {cmd}\n"})
    link = _link(infra.run(tmp_path)["nodes"][0])
    assert (link["file"], link["status"]) == (want, "strong_inference")
    assert link["evidence"][1]["at"] == "Dockerfile:3"


def test_a_compose_build_written_as_a_flow_mapping(tmp_path):
    _project(tmp_path, {"app/main.py": "x = 1\n",
                        "Dockerfile": 'FROM python\nCOPY . .\nCMD ["python", "app/main.py"]\n',
                        "docker-compose.yml": "services:\n  api:\n    build: {context: ., dockerfile: Dockerfile}\n"})
    node = _node(infra.run(tmp_path), "compose_service", "api")
    assert node["build"]["context"] == "." and "why" not in node["build"]
    assert (_link(node)["file"], _link(node)["status"]) == ("app/main.py", "strong_inference")


def test_a_quoted_bracket_does_not_swallow_the_rest_of_the_document():
    doc = infra.yaml_docs('spec:\n  containers:\n  - name: c\n    args: ["[", "a"]\n  - name: d\n'
                          '    command: ["python", "m.py"]\n')[0]
    assert [c.get("name").v for c, _ in infra._containers(doc, [])] == ["c", "d"]


def test_a_server_app_is_not_cited_at_the_main_guard(tmp_path):
    _project(tmp_path, {"app/main.py": "from app.factory import make\nfrom app.web import app\n\n"
                                       "if __name__ == '__main__':\n    import uvicorn\n",
                        "Dockerfile": 'FROM python\nCOPY . .\nCMD ["uvicorn", "app.main:app"]\n'})
    assert _link(infra.run(tmp_path)["nodes"][0])["line"] == 2      # the import that brings app


@pytest.mark.parametrize("argv, want", [
    (["./wait-for-it.sh", "db:5432", "--", "python", "app.py"], [("path", "./wait-for-it.sh"), ("path", "app.py")]),
    (["poetry", "run", "python", "-m", "app.main"], [("module", "app.main")]),
    (["uv", "run", "--frozen", "app.py"], [("path", "app.py")]),
])
def test_launchers_and_wait_scripts(argv, want):
    assert infra.runs(argv) == want


def test_byte_order_marks_and_dockerignore_files(tmp_path):
    _project(tmp_path, {"app/__init__.py": "", "app/main.py": "x = 1\n",
                        "Dockerfile": 'FROM python\nWORKDIR /app\nCOPY app/ app/\nCMD ["python", "-m", "app.main"]\n',
                        "pod.yaml": "apiVersion: v1\nkind: Pod\nmetadata:\n  name: p\nspec:\n  containers:\n"
                                    "  - name: c\n    command: [\"python\", \"app/main.py\"]\n",
                        "compose.yml": "services:\n  web:\n    command: python app/main.py\n",
                        "main.tf": 'resource "null_resource" "r" {\n}\n'}, bom=True)
    _project(tmp_path, {"Dockerfile.dockerignore": "node_modules\n"})
    res = infra.run(tmp_path)
    assert {n["kind"] for n in res["nodes"]} == {"dockerfile", "k8s_container", "compose_service",
                                                 "terraform_resource"}
    assert res["linked"] == 3 and res["files_read"]["dockerfile"] == 1


def test_a_file_run_twice_is_one_node_and_one_link(tmp_path):
    _project(tmp_path, {"app/main.py": "x = 1\n",
                        "Dockerfile": 'FROM python\nCOPY app/ app/\nCMD ["sh", "-c", "python app/main.py && '
                                      'python app/main.py"]\n'})
    res = infra.run(tmp_path)
    assert len(res["nodes"][0]["runs"]) == 1 and len(infra.links_for(res, "app/main.py")) == 1


def test_cli_file_is_repository_relative_first_and_a_missing_one_is_an_error(tmp_path, capsys, monkeypatch):
    e = _project(tmp_path / "e", {"app/main.py": "x = 1\n", "other.py": "y = 1\n",
                                  "Dockerfile": 'FROM python\nCOPY . .\nCMD ["python", "app/main.py"]\n'})
    other = _project(tmp_path / "other", {"app/main.py": "z = 1\n"})
    monkeypatch.chdir(other)
    assert cli.main(["infra", "--repo", str(e), "--file", "app/main.py", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert cli.main(["infra", "--repo", str(e), "--file", "nope.py"]) == 2
    capsys.readouterr()
    assert cli.main(["infra", "--repo", str(e), "--file", "other.py"]) == 1
    assert "no node runs or names other.py" in capsys.readouterr().out
