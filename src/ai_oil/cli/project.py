"""`oil project` and `oil component`: projects and their components (DM-1, DM-3)."""

import argparse

from ai_oil.cli.common import transaction
from ai_oil.domain import projects
from ai_oil.governance import audit


def register(subparsers) -> None:
    project = subparsers.add_parser("project", help="manage projects")
    project_sub = project.add_subparsers(dest="project_command", required=True)

    add = project_sub.add_parser("add", help="create a project")
    add.add_argument("key", metavar="KEY", help="project key, 2-10 upper-case letters and digits")
    add.add_argument("name", metavar="NAME", help="project name")
    add.add_argument("--description", default=None, help="project description")
    add.set_defaults(func=run_project_add)

    list_ = project_sub.add_parser("list", help="list projects")
    list_.add_argument("--all", action="store_true", help="include archived projects")
    list_.set_defaults(func=run_project_list)

    component = subparsers.add_parser("component", help="manage components")
    component_sub = component.add_subparsers(dest="component_command", required=True)

    add = component_sub.add_parser("add", help="add a component to a project")
    add.add_argument("project", metavar="PROJECT", help="project key")
    add.add_argument("name", metavar="NAME", help="component name")
    add.add_argument("--description", default=None, help="component description")
    add.set_defaults(func=run_component_add)

    list_ = component_sub.add_parser("list", help="list the components of a project")
    list_.add_argument("project", metavar="PROJECT", help="project key")
    list_.set_defaults(func=run_component_list)


def run_project_add(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        project = projects.create_project(conn, args.key, args.name, description=args.description)
        audit.log(
            conn,
            "project.create",
            "B",
            actor_type="user",
            target=project.key,
            detail={"name": project.name, "description": args.description},
        )
    print(f"Created project {project.key}")
    return 0


def run_project_list(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        listed = projects.list_projects(conn, include_archived=args.all)
    for project in listed:
        print(f"{project.key}  {project.name}" + ("  (archived)" if project.archived else ""))
    return 0


def run_component_add(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        project = projects.get_project(conn, args.project)
        component = projects.create_component(conn, project.id, args.name, args.description)
        audit.log(
            conn,
            "component.create",
            "B",
            actor_type="user",
            target=project.key,
            detail={"component": component.name, "description": args.description},
        )
    print(f"Created component {component.name} in {project.key}")
    return 0


def run_component_list(args: argparse.Namespace) -> int:
    with transaction(args) as conn:
        project = projects.get_project(conn, args.project)
        listed = projects.list_components(conn, project.id)
    for component in listed:
        print(component.name)
    return 0
