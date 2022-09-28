#!/usr/bin/env python3

"""
This script provides useful utilities for working with Judo project source code.
To install make sure you are in the judo-ng directory.

1a. Install with pyenv. 

    
   Install pyenv in Linux (https://github.com/pyenv/pyenv-installer)

       curl https://pyenv.run | bash
       exec $SHELL

   Install on macOS (https://github.com/pyenv/pyenv)

       brew update
       brew install pyenv
       
   Install on Windows: https://github.com/pyenv-win/pyenv-win#installation


   Add virtual environment:
 
       pyenv virtualenv 3.7.3 judo-ng
       pyenv local judo-ng
       
   After installation it is activated, nothing to do.

1b. Install without pyenv

   Install python and dependencies (linux):

      sudo apt install python3
      sudo apt install python3-venv
      sudo apt install python-pip
      sudo apt install graphviz


   Create virtual environment (to not change global python version and libraries) and install dependencies

      python3 -m venv .pyenv

   Later, when interacting with the script, make sure you activated the environment with:
    
      source .pyenv/bin/activate


2. Install requirements

      pip install wheel
      pip install -r requirements.txt


Those features that use the remote repository need a GitHub token for authentication, 
visit https://github.com/settings/tokens to create one. Scope to select: repo - Full control of private repositories.

A very important output of the script is the project-meta.yml file. 
This contains the relations between the modules and is used to update pom.xml files etc.
Try it:

    ./project.py -sg

will create an SVG file showing the dependencies between the projects.

== Update project-meta.yml to contain the latest version in the remote repository

    ./project.py -fv -gh <YOUR_GITHUB_TOKEN_HERE>

The -fv option will check the latest release of all the modules and update the project-meta.yml file accordingly.
This information then can be used to update pom.xml files to use the latest versions of the modules.

== Change the dependencies of a given module to the latest versions
Assuming you've already updated the versions in the project-meta.yml file, you can update one module's dependencies to
the latest versions:

    ./project.py -ump <module name>

You can do the fetching and the updating in one step:

    ./project.py -fv -ump <module_name> -gh <github_token>

== Execute build locally with module and modules depending on it recursively to snapshot version

Calling the script with the -bs option will execute a local build with SNAPSHOT version. The other modules
will use the versions is defined in their pom.xml

    ./project.py -bs

It starts a build for all modules which is not virtual or ignored by default.
With the -bm switch the modules where the build starts from can be defined. In that case
the defined modules and all dependent modules starts to build.
To ignore specific modules, use -bi switch.

If the build is failing somewhere, after fixing the issue you can continue

    ./project.py -bs -bc <module_to_continue_from>

To start a build fromn judo-meta-jsl, type

    ./project.py -bs -bm judo-meta-jsl


= Releasing using the script
Always release from a separate local repository, not your working copy.

For example clone again:

    git clone --recurse-submodules git@github.com:BlackBeltTechnology/judo-ng.git ~/rel-judo-ng

Make sure everything locally is up to date:

    git submodule init
    git submodule update --recursive
    ./project.py -fv -gc -gh $(cat ~/githubtoken)

You can switch all submodules to the project-meta defined branches and pull it 
(it stash all uncommitted changes):

    ./project.py -us

Switch to the correct tatami branch:

    cd runtime/judo-tatami
    git checkout develop
    cd ../..

Start the builds:

    ./project.py -pu -up -cu -gh  $(cat ~/githubtoken)

When working an HTTP server run, you can see the process:

    http://localhost:8000

"""
import json
import os
import re
from argparse import RawDescriptionHelpFormatter, ArgumentParser

from github import Github
import yaml
from xml.etree.ElementTree import Comment, register_namespace, parse, XMLParser, TreeBuilder
from subprocess import call, check_output

import time
import shutil
from git import RemoteProgress

import networkx as nx
from networkx.drawing.nx_pydot import write_dot, to_pydot
from networkx.algorithms.dag import transitive_reduction
from networkx.algorithms.dag import ancestors, descendants

import argparse
import textwrap

import threading
import http.server
import atexit
from datetime import datetime

from colorama import init, Fore, Style

from atlassian import Jira

init(autoreset=True)

# noinspection PyTypeChecker
parser: ArgumentParser = argparse.ArgumentParser(formatter_class=RawDescriptionHelpFormatter,
                                                 description=textwrap.dedent(
                                                     "Handling module building of Judo NG\n\n" + __doc__))

general_arg_group = parser.add_argument_group('General arguments')
general_arg_group.add_argument("-sg", "--graphviz", action="store_true", dest="graphviz", default=False,
                               help='Save graphviz representation of current state')
general_arg_group.add_argument("-d", "--dirty", action="store_true", dest="dirty", default=False,
                               help='Do not update yaml')
general_arg_group.add_argument("-cu", "--continous", action="store_true", dest="continous_update", default=False,
                               help='Continuously update / fetch / wait until last level')
general_arg_group.add_argument("-minr", "--minrank", action="store", dest="min_rank", default=1, type=int,
                               help='Minimum rank is run')
general_arg_group.add_argument("-maxr", "--maxrank", action="store", dest="max_rank", default=100, type=int,
                               help='Maximum rank is run')
general_arg_group.add_argument("-p", "--project", action="store", dest="project", default=None,
                               help='Run only on the defined project')
general_arg_group.add_argument("-relnotes", "--release-notes", dest="relnotes", nargs=1,
                               metavar=("FROM_HASH[..TO_HASH]"),
                               help="Generate release notes based on different project-meta.yml versions")

# parser.add_argument("-ss", "--snapshots", nargs='*', dest="switch_to_snapshots",
#                    help="Switch to snapshot dependencies starting from the given module(s). If not given all modules is building.")
# parser.add_argument("-bl", "--build-local", dest="build_local", metavar='CONTINUE_FROM', nargs='?', const='False',
#                    help="Build updated modules locally")
# parser.add_argument("-bp2", "--build-local-p2", action="store_true", dest="build_local_p2", default=False,
#                    help='Build p2 repository for local build')

localbuild_arg_group = parser.add_argument_group('Local build control arguments')
localbuild_arg_group.add_argument("-bs", "--build-snapshot", dest="build_snapshot", action='store_true', default=False,
                                  help="Build modules")
localbuild_arg_group.add_argument("-bi", "--build-ignore", dest="build_modules_ignored", metavar='MODULE...',
                                  nargs="*",
                                  help="Ignore given module(s)")
localbuild_arg_group.add_argument("-bm", "--build-modules", dest="build_modules",
                                  metavar='MODULE...', nargs="*",
                                  help="Build the given module(s) and ascendants")
localbuild_arg_group.add_argument("-bc", "--build-continue-from", dest="continue_module",
                                  metavar='MODULE', nargs=1,
                                  help="Retry build from the given module")

git_arg_group = parser.add_argument_group('GIT control arguments')
git_arg_group.add_argument("-gc", "--gitcheckout", action="store_true", dest="git_checkout", default=False,
                           help='Fetch / Reset / Checkout branch')
git_arg_group.add_argument("-nogc", "--no-gitcheckout", nargs='*', dest="no_checkout_module",
                           help="Don't checkout the given modules")
git_arg_group.add_argument("-pu", "--pushupdates", action="store_true", dest="push_updates", default=False,
                           help='Push updates in projects')
git_arg_group.add_argument("-noci", "--noci", action="store_true", dest="ci_skip", default=False,
                           help='Make commit with CI Ignore')
git_arg_group.add_argument("-us", "--updatesubmodules", action="store_true", dest="update_submodules", default=False,
                           help='Update submodules branches')

github_arg_group = parser.add_argument_group('GitHub API control arguments')
github_arg_group.add_argument("-fv", "--fetchversions", action="store_true", dest="fetch_github_versions",
                              default=False,
                              help='Fetch last released versions from github')

# parser.add_argument("-wu", "--wait", action="append", dest="wait_formodules", default=[],
# 	help='Repeated wait for the updated modules version update')
# parser.add_argument("-wi", "--waitinterval", action="store", dest="wait_interval", type=int, default=60,
# 	help='How many sec is waited between the version fetches')
# parser.add_argument("-wt", "--waittimeout", action="store", dest="wait_timeout", type=int, default=3600,
# 	help='When the whole repeated wait is time outed')

pom_arg_group = parser.add_argument_group('Maven POM control arguments')
pom_arg_group.add_argument("-up", "--updatepom", action="store_true", dest="update_pom", default=False,
                           help='Update pom.xml')
pom_arg_group.add_argument("-ump", "--updatemodulepom", nargs=1, dest="update_module_pom",
                           help='Update pom.xml of one module')
pom_arg_group.add_argument("-rp", "--runpostchangescripts", action="store_true", dest="run_postchangescripts",
                           default=False,
                           help='Run postchange script without version update')

access_arg_group = parser.add_argument_group('Access settings')
access_arg_group.add_argument("-gh", "--githubtoken", action="store", dest="github_token",
                              default=os.environ.get('JUDO_GITHUB_TOKEN', ''),
                              help='GitHub token used for authentication')
access_arg_group.add_argument("-jtok", "--jiratoken", action="store", dest="jira_token",
                              default=os.environ.get('JUDO_JIRA_TOKEN', ''),
                              help='Jira token used for authentication (https://id.atlassian.com/manage-profile/security/api-tokens)')
access_arg_group.add_argument("-jusr", "--jirauser", action="store", dest="jira_user",
                              default=os.environ.get('JUDO_JIRA_USER', ''),
                              help='Jira user used for authentication - same user as token user')

args = parser.parse_args()

current_rank = args.min_rank

modules = []
module_by_name = {}
process_info = {}


class StoppableHTTPServer(http.server.HTTPServer):
    def run(self):
        try:
            self.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.server_close()


process_info_server: StoppableHTTPServer = None
process_info_server_pid = None

register_namespace('', 'http://maven.apache.org/POM/4.0.0')
pom_namespace = "{http://maven.apache.org/POM/4.0.0}"


class CloneProgress(RemoteProgress):
    def update(self, op_code, cur_count, max_count=None, message=''):
        if message:
            print(message)


# noinspection PyTypeChecker
class CommentedTreeBuilder(TreeBuilder):
    def __init__(self, *arguments, **kwargs):
        super(CommentedTreeBuilder, self).__init__(*arguments, **kwargs)

    def comment(self, data):
        self.start(Comment, {})
        self.data(data)
        self.end(Comment)


class Module(object):
    def __init__(self, init_dict):
        self.name = init_dict['name']
        self.url = init_dict['url']

        self.path = None
        if 'path' in init_dict:
            self.path = init_dict['path']

        self.branch = init_dict['branch']
        self.property = init_dict['property']
        self.rank = 1
        if 'version' in init_dict:
            self.version = init_dict['version']  # .encode("ascii", "ignore")
        self.github = init_dict['github']
        self.dependencies = []
        if 'dependencies' in init_dict:
            self.dependencies = init_dict['dependencies']

        self.afterversionchange = []
        if 'afterversionchange' in init_dict:
            self.afterversionchange = init_dict['afterversionchange']

        self.beforelocalbuild = []
        if 'beforelocalbuild' in init_dict:
            self.beforelocalbuild = init_dict['beforelocalbuild']

        self.afterlocalbuild = []
        if 'afterlocalbuild' in init_dict:
            self.afterlocalbuild = init_dict['afterlocalbuild']

        self.p2 = {}
        if 'p2' in init_dict:
            self.p2 = init_dict['p2']

        self.ignored = False
        if 'ignored' in init_dict:
            self.ignored = init_dict['ignored']

        self.virtual = False
        if 'virtual' in init_dict:
            self.virtual = init_dict['virtual']

    # if self.branch == 'master':
    #    self.ignored = True

    def __repr__(self):
        return self.name + " (" + str(self.rank) + ")"

    def resolve_dependencies(self, _module_by_name):
        dependencies = []
        if type(self.dependencies) is list:
            for reference in self.dependencies:
                dependencies.append(_module_by_name[reference])
        elif type(self.dependencies) is str:
            dependencies.append(_module_by_name[self.dependencies])
        else:
            raise SystemExit(f"{Fore.RED}Error: references have to be list or str type")
        self.dependencies = dependencies

    def deresolve_dependencies(self):
        dependencies = []
        for dependency in self.dependencies:
            dependencies.append(dependency.name)
        self.dependencies = dependencies

    def update_github_versions(self):
        if not self.is_process():
            return
        github = Github(login_or_token=args.github_token)
        # repository = github.get_organization(par['github'].split("/")[0]).get_repo(par['github'].split("/")[1])
        repository = github.get_repo(self.github)
        if self.branch == 'master':
            for tag in repository.get_tags():
                if (tag.name and re.match(r'^v(\d+\.)?(\d+\.)?(\*|\d+)$', tag.name)):
                    ver = tag.name.strip()[1:]  # .encode('ascii', 'ignore')
                    if self.version != ver:
                        print(
                            f"{Fore.YELLOW}Updating release version of {Fore.GREEN}{self.name}{Fore.YELLOW}: "
                            f"{Fore.GREEN}{self.version} {Fore.YELLOW}=>{Fore.GREEN} {ver}")
                        self.version = ver
                        return True
                    else:
                        return False
        else:
            for tag in repository.get_tags():
                print(" -> " + tag.name)
                if (tag.name and re.match(r'^v.*' + self.branch + '.*', tag.name)):
                    ver = tag.name.strip()[1:]  # .encode('ascii', 'ignore')                  
                    if self.version != ver:
                        print(
                            f"{Fore.YELLOW}Updating version of {Fore.GREEN}{self.name}{Fore.YELLOW}: "
                            f"{Fore.GREEN}{self.version} {Fore.YELLOW}=>{Fore.GREEN} {ver}")
                        self.version = ver
                        return True
                    else:
                        return False
        return False

    def update_dependency_versions_in_pom(self, write_pom=False):
        if self.path is None:
            return

        if not self.is_process():
            return

        print(f"{Fore.YELLOW}Checking POM: {Fore.GREEN}{self.path}/pom.xml")
        pom = parse(open(self.path + "/pom.xml", encoding="UTF-8"),
                    parser=XMLParser(target=CommentedTreeBuilder()))
        root = pom.getroot()
        properties_element = root.find(pom_namespace + 'properties')
        if properties_element is None:
            raise SystemExit(f"{Fore.RED}Error Any reference version have to be in properties definition on pom.xml: "
                             f"{self.name}")

        update = False
        for dependency in self.dependencies:
            ref_prop_element = properties_element.find(pom_namespace + dependency.property)
            # print(" ---> Dependency: " + dependency.name + " " + dependency.version)
            if ref_prop_element is None:
                raise SystemExit(f"{Fore.RED}{dependency.property} have to be defined in properties on pom.xml: "
                                 f"{self.name}")
            if ref_prop_element.text != dependency.version:
                ref_prop_element.text = dependency.version
                update = True
        # print(pom)
        if update:
            if write_pom:
                print("     Writing POM")
                pom.write(self.path + "/pom.xml", encoding="UTF-8")
            return True
        return False

    def call_postchangescripts(self):
        _currentDir = os.getcwd() + '/' + self.path
        for postchangescript in self.afterversionchange:
            if call(postchangescript, shell=True, cwd=_currentDir) != 0:
                return False
        return True

    def call_afterlocalbuildscripts(self):
        _currentDir = os.getcwd() + '/' + self.path
        for afterlocalbuildscript in self.afterlocalbuild:
            if call(afterlocalbuildscript, shell=True, cwd=_currentDir) != 0:
                return False
        return True

    def call_beforelocalbuildscripts(self):
        _currentDir = os.getcwd() + '/' + self.path
        for beforelocalbuildscript in self.beforelocalbuild:
            if call(beforelocalbuildscript, shell=True, cwd=_currentDir) != 0:
                return False
        return True

    def checkout_branch(self):
        _currentDir = os.getcwd() + '/' + self.path
        print(f"{Fore.YELLOW}Checkout: " + self.path)
        call("git stash", shell=True, cwd=_currentDir)
        call("git checkout " + self.branch, shell=True, cwd=_currentDir)
        call("git pull", shell=True, cwd=_currentDir)

    def commit_and_push_changes(self):
        _currentDir = os.getcwd() + '/' + self.path
        print(f"{Fore.YELLOW}Commit and push: " + self.path)
        call("git add .", shell=True, cwd=_currentDir)

        _commit_message = "[Release] Updating versions"
        if args.ci_skip:
            _commit_message = _commit_message + " [ci skip]"
        call("git commit -m \"" + _commit_message + "\"", shell=True, cwd=_currentDir)
        call("git push", shell=True, cwd=_currentDir)

    def is_process(self):
        return not self.ignored and (
                not args.project or args.project == self.name) and args.min_rank <= self.rank <= args.max_rank


def process_module(par, _modules, _module_by_name):
    if type(par) is dict:
        _new_module = Module(par)
        _modules.append(_new_module)
        _module_by_name[_new_module.name] = _new_module
    elif type(par) is list:
        for _item in par:
            process_module(_item, _modules, _module_by_name)


def load_modules(filename="project-meta.yml"):
    with open(filename, 'r') as stream:
        try:
            _results = yaml.load(stream, Loader=yaml.FullLoader)
            return _results
        except yaml.YAMLError as exc:
            print(exc)
            raise exc


def calculate_graph(_modules):
    _g = nx.DiGraph()
    for _module in _modules:
        _g.add_node(_module)

    for _module in _modules:
        for dependency in _module.dependencies:
            _g.add_edge(_module, dependency)
    return _g


def calculate_reduced_graph(_modules):
    _gt = transitive_reduction(calculate_graph(_modules))
    return _gt


def calculate_ranks(_modules):
    _g = calculate_reduced_graph(_modules).reverse(copy=True)
    for node in _g.nodes:
        node.rank = 1

    for node in _g.nodes:
        for (head, tail) in nx.bfs_edges(_g, node):
            tail.rank = max(tail.rank, head.rank + 1)

    # Another round because on same condition parent / child can have same rank.
    for node in _g.nodes:
        for (head, tail) in nx.bfs_edges(_g, node):
            tail.rank = max(tail.rank, head.rank + 1)


def scrub_dict(d):
    new_dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = scrub_dict(v)
        if isinstance(v, list):
            v = scrub_list(v)
        if not v in (u'', None, {}, []):
            new_dict[k] = v
    return new_dict


def scrub_list(d):
    scrubbed_list = []
    for i in d:
        if isinstance(i, dict):
            i = scrub_dict(i)
        scrubbed_list.append(i)
    return scrubbed_list


def save_modules(_modules, _module_by_name):
    print(f"{Fore.YELLOW}Saving yaml")
    _export = []
    for _module in _modules:
        _module.deresolve_dependencies()
        _export.append(scrub_dict(vars(_module)))

    with open("project-meta.yml", 'w') as yaml_file:
        try:
            yaml.dump(_export, yaml_file, default_flow_style=False, allow_unicode=True)
        except yaml.YAMLError as exc:
            print(exc)
            raise exc

    for _module in _modules:
        _module.resolve_dependencies(_module_by_name)


def print_dependency_graph(_modules):
    # https://ocefpaf.github.io/python4oceanographers/blog/2014/11/17/networkX/
    # write_dot(calculate_reduced_graph(), "dependency.dot")
    # (graph,) = pydot.graph_from_dot_data(open('dependency.dot').read())
    # graph.write_svg("dependency.svg")
    _g = calculate_reduced_graph(_modules)
    for node in _g.nodes():
        _g.nodes[node]['shape'] = 'box'
        _g.nodes[node]['label'] = f"{node.name} ({node.rank})"
    write_dot(_g, "dependency.dot")
    to_pydot(_g).write_svg("dependency.svg")


class MyHandler(http.server.BaseHTTPRequestHandler):
    modules = None
    process_info = None

    def __init__(self, _modules, _process_info):
        self.modules = _modules
        self.process_info = _process_info

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_GET(self):

        _g = nx.DiGraph()
        for _module in self.process_info.keys():
            _g.add_node(_module)

        for _module in self.modules:
            for dependency in _module.dependencies:
                if dependency in self.process_info.keys():
                    _g.add_edge(_module, dependency)

        _g = transitive_reduction(_g)
        for node in _g.nodes():
            _g.node[node]['shape'] = 'box'
            _g.node[node]['label'] = f"{node.name} ({node.rank})"
            _g.node[node]['fillcolor'] = 'azure3'
            _g.node[node]['style'] = 'filled'
            if self.process_info.get(node, {"status": "UNKNOWN"}).get("status") == "UNKNOWN":
                _g.node[node]['fillcolor'] = 'wheat'
            if self.process_info.get(node, {"status": ""}).get("status") == "RUNNING":
                _g.node[node]['fillcolor'] = "yellow"
            if self.process_info.get(node, {"status": ""}).get("status") == "OK":
                _g.node[node]['fillcolor'] = "green"

        svg = to_pydot(_g).create_svg().decode("utf-8")
        # body = "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head><title/></head><body><svg>" + svg +
        # "</svg></body</html>"
        self.send_response(200)
        # self.send_header("Content-type", "text/html; charset=utf-8")
        self.send_header("Content-type", "image/svg+xml; charset=utf-8")

        self.send_header("Content-Length", str(len(svg)))
        self.end_headers()
        self.wfile.write(bytes(svg, "utf-8"))


# return StringIO(body)


def start_process_info_server(_modules, _process_info):
    handler = MyHandler(_modules, _process_info)
    protocol = "HTTP/1.1"
    port = 8000
    server_address = ('localhost', port)
    handler.protocol_version = protocol
    httpd: StoppableHTTPServer = StoppableHTTPServer(server_address, handler)
    print(f"{Fore.YELLOW}Serving at port:{Fore.GREEN}{port}")
    return httpd


def wait_for_modules_to_release(_process_info, _wait_for=None):
    if _wait_for is None:
        _wait_for = []
    while len(_wait_for) > 0:
        time.sleep(15)
        for _module in _wait_for:
            _process_info[_module] = {"stat us": "RUNNING"}
            print(
                f"{Fore.YELLOW}Checking latest release for {Fore.GREEN}{_module.name}{Fore.YELLOW} in branch: "
                f"{Fore.GREEN}{_module.branch}")
            if _module.update_github_versions():
                print("  NEW Version, it removed from wait list")
                _process_info[_module] = {"status": "OK"}
                _wait_for.remove(_module)


def update_current_rank(_modules, _module_by_name):
    _updated_dependency_in_modules = []
    for _module in _modules:
        if _module.is_process():
            # print("Checking dependency update in POM: " + module.name)
            if _module.update_dependency_versions_in_pom(False):
                _updated_dependency_in_modules.append(_module)

    minimum_updated_rank = 1000000

    for _module in _updated_dependency_in_modules:
        if minimum_updated_rank > _module.rank >= args.min_rank and _module.rank <= args.max_rank:
            minimum_updated_rank = _module.rank

    _current_updated_dependency_in_modules = []
    for _module in _updated_dependency_in_modules:
        # print(module.name + " module update in this batch? " + ("YES" if minimum_updated_rank == module.rank else
        # "NO"))
        if minimum_updated_rank == _module.rank:
            _current_updated_dependency_in_modules.append(_module)

    _current_rank = minimum_updated_rank
    print("Current rank: " + str(_current_rank))

    for _module in _current_updated_dependency_in_modules:
        if args.update_pom:
            print(f"{Fore.YELLOW}Writing changes to {Fore.GREEN}{_module.name}")
            _module.update_dependency_versions_in_pom(True)

        if args.update_pom and not args.run_postchangescripts:
            print("Running post change scripts")
            if not _module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {_module.name}.")
                if not args.dirty:
                    save_modules(_modules, _module_by_name)
                exit(1)
        if args.push_updates:
            print("Pushing updates to " + _module.name)
            _module.commit_and_push_changes()
    return _current_updated_dependency_in_modules


# =============================== Initial version fetching

def fetch_github_versions():
    for _module in modules:
        if _module.is_process():
            print(
                f"{Fore.YELLOW}Checking latest release for {Fore.GREEN}{_module.name}{Fore.YELLOW} in branch: "
                f"{Fore.GREEN}{_module.branch}")
            _module.update_github_versions()


def current_snapshot_version(_module):
    pom = parse(open(_module.path + "/pom.xml"), parser=XMLParser(target=CommentedTreeBuilder()))
    root = pom.getroot()
    properties = list(root.find(pom_namespace + 'properties'))
    _version = None
    for e in properties:
        if e.tag == pom_namespace + "revision":
            _version = e.text
            break
    if _version is None:
        _version = root.find(pom_namespace + "version").text

    return _version

def current_pom_version(_module, _dep_module):
    pom = parse(open(_module.path + "/pom.xml"), parser=XMLParser(target=CommentedTreeBuilder()))
    root = pom.getroot()
    properties = list(root.find(pom_namespace + 'properties'))
    _version = None
    for e in properties:
        if e.tag == pom_namespace + _dep_module.property:
            _version = e.text
            break

    return _version


def build_module(_module, arguments=None):
    if arguments is None:
        arguments = []
    print(f"\n{Fore.YELLOW}================================================================================{Style.RESET_ALL}")
    print(f"\n{Fore.YELLOW}Building module{Style.RESET_ALL} {Fore.RED}{_module.path}{Style.RESET_ALL}")
    # proc = subprocess.Popen(["mvn", "clean", "install"] + args +
    # ["-f", f"{module.path}/pom.xml"], stdout=subprocess.PIPE)
    # for line in proc.stdout:
    # 	print(line.decode('UTF-8'), end="")
    # proc.wait()
    # if proc.returncode != 0:
    # 	return False
    # return True
    _currentDir = os.getcwd() + '/' + _module.path
    if not _module.call_beforelocalbuildscripts():
        return False
    cmd = "mvn clean install " + (" ".join(list(map(lambda x: f"-D{x}", arguments))))
    print(f"Calling maven: \n{Fore.MAGENTA}{cmd}{Style.RESET_ALL}\n")
    print(f"\n{Fore.YELLOW}================================================================================{Style.RESET_ALL}")
    if call(cmd, shell=True, cwd=_currentDir) != 0:
        return False
    if not _module.call_afterlocalbuildscripts():
        return False
    return True


def build_snapshot(_modules, _process_info, _module_by_name):
    _available_modules = set(filter(lambda _m: not _m.virtual and not _m.ignored, _modules))
    _ignored_modules = set()
    if not args.build_modules_ignored is None:
        for _module_name in args.build_modules_ignored:
            _module = _module_by_name[_module_name]
            if _module is None:
                raise SystemExit(f"\n{Fore.RED}No module name is found {_module_name}.")
            if _module.virtual:
                raise SystemExit(f"\n{Fore.RED}Virtual module cannot be used in snapshot mode: {_module_name}.")
            _ignored_modules.add(_module)

    _retry_command = "./project.py -bs "

    if len(_ignored_modules) > 0:
        print(f"{Fore.YELLOW}Modules ignored:{Style.RESET_ALL} " + (
            ", ".join(map(lambda _m: _m.name, list(_ignored_modules)))) + "\n")
        _retry_command = _retry_command + "-bi "
        for _module in _ignored_modules:
            _retry_command = _retry_command + _module.name + " "

    _root_modules = set()
    if not args.build_modules is None:
        for _module_name in args.build_modules:
            _module = _module_by_name[_module_name]
            if _module is None:
                raise SystemExit(f"\n{Fore.RED}No module name is found {_module_name}.")
            if _module.virtual:
                raise SystemExit(f"\n{Fore.RED}Virtual module cannot be used in snapshot mode: {_module_name}.")
            _root_modules.add(_module)

    _modules_to_build = set(_available_modules)

    if len(_root_modules) > 0:
        _retry_command = _retry_command + "-bm "
        _modules_to_build = set(_root_modules)
        print(f"{Fore.YELLOW}Root modules:{Style.RESET_ALL} " + (
            ", ".join(map(lambda _m: _m.name, list(_root_modules)))))
        for _module in _root_modules:
            print(f"{Fore.BLUE}{_module.name} {Style.RESET_ALL} ancestors added: " + (
                ", ".join(
                    map(lambda _m: _m.name, ancestors(calculate_reduced_graph(_available_modules), _module)))))
            _modules_to_build = _modules_to_build.union(set(ancestors(calculate_reduced_graph(_available_modules), _module)))
            _retry_command = _retry_command + _module.name + " "

    _modules_to_build = sorted(_modules_to_build.difference(_ignored_modules), key=lambda _m: (_m.rank, _m.name))
    print(f"\n{Fore.YELLOW}Modules to build:{Style.RESET_ALL} " + (
        ", ".join(map(lambda _m: _m.name, _modules_to_build))) + "\n")

    _version_args = []
    for _module in _available_modules.difference(_ignored_modules) :
        _version_args.append(_module.property + "=" + current_snapshot_version(_module))

    print(f"{Fore.YELLOW}Versions override: {Fore.GREEN}" + f"{Fore.YELLOW}, {Fore.GREEN}".join(_version_args) + "\n")

    if args.continue_module:
        _start_module_name = args.continue_module[0]
        _build_from_index = list(map(lambda _m: _m.name, _modules_to_build)).index(_start_module_name)
        _modules_to_build = list(_modules_to_build[_build_from_index:])

    for _module in _modules_to_build:
        _process_info[_module] = {"status": "RUNNING"}

        if _module.p2 and 'target' in _module.p2.keys():
            with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['releaselocations'])) as f:
                _releaseLocs = {k: v for _line in filter(str.rstrip, f) for (k, v) in [_line.strip().split(None, 1)]}

            _locations = dict(_releaseLocs)
            for _dependency_module in _module.dependencies:
                _locations_key = _dependency_module.name + "-location"
                _version = current_pom_version(_module, _dependency_module)
                _value = None

                if _dependency_module in _available_modules:
                    _value = "file:" + os.path.abspath(os.getcwd() + "/" + _dependency_module.path + "/" + _dependency_module.p2['localsite'])
                elif _locations_key in _releaseLocs.keys():
                    _value = _releaseLocs[_locations_key]

                if _value:
                    _locations[_locations_key] = _value.replace("${" + _dependency_module.property + "}", _version)

            print("P2 Locations: \n" + json.dumps(_locations, indent=4))

            with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['template'])) as _tf:
                _template = "".join(_tf.readlines()).replace("${build.timestamp.millis}", str(int(time.time() * 1000)))
                for _loc in _locations.keys():
                    _template = _template.replace("${" + _loc + "}", _locations[_loc])
                with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['target']), 'w', encoding='utf-8') as _out:
                    _out.write(_template)

        if not build_module(_module, _version_args):
           raise SystemExit(f"\n{Fore.RED}Error when building {_module.name}. "
                            f"To retry, run {_retry_command} -bc {_module.name}")
        _process_info[_module] = {"status": "OK"}


def server_start(_modules, _process_info):
    global process_info_server
    global process_info_server_pid
    process_info_server = start_process_info_server(_modules, _process_info)
    process_info_server_pid = threading.Thread(None, target=process_info_server.run)
    process_info_server_pid.setDaemon(True)
    process_info_server_pid.start()


def server_shutdown():
    global process_info_server
    global process_info_server_pid

    print(f"{Fore.YELLOW}Exiting()")
    if process_info_server is not None:
        process_info_server.shutdown()

    if process_info_server_pid is not None:
        process_info_server_pid.join()


# ============================== Check argument
if args.continous_update and args.project:
    raise SystemExit(f"{Fore.ERROR}Continuous update cannot be use when project is filtered")

# ============================== Mandatory module init
print(f"{Fore.YELLOW}Load modules")
for item in load_modules():
    process_module(item, modules, module_by_name)

for module in modules:
    # print("Resolve dependencies: " + module.name)
    module.resolve_dependencies(module_by_name)

calculate_ranks(modules)

# =============================== Start process info server
# atexit.register(server_shutdown)
# server_start(modules, process_info)

# =============================== Fetch / Checkout / Reset Git
if args.git_checkout:
    for module in modules:
        if not module.virtual and not (args.no_checkout_module and module.name in args.no_checkout_module):
            module.checkout_branch()

if args.fetch_github_versions:
    fetch_github_versions()

if args.update_module_pom:
    for module in modules:
        if module.name == args.update_module_pom[0]:
            module.update_dependency_versions_in_pom(True)
            if not module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {module.name}.")
                if not args.dirty:
                    save_modules(modules, module_by_name)
                raise SystemExit(1)

# =============================== Save YAML
if not args.dirty:
    save_modules(modules, module_by_name)

# =============================== Switch to snapshot versions from the given module
if args.build_snapshot:
    build_snapshot(modules, process_info, module_by_name)

# =============================== Generating Graphviz
if args.graphviz:
    print_dependency_graph(modules)

# =============================== Force postchange run
if args.run_postchangescripts:
    for module in modules:
        if module.is_process():
            if not module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {module.name}.")
                if not args.dirty:
                    save_modules(modules, module_by_name)
                exit(1)
            if args.push_updates:
                module.commit_and_push_changes()

# =============================== Update submodules
if args.update_submodules:
    currentDir = os.getcwd()

    for module in modules:
        if not module.virtual:
            print(f"{Fore.YELLOW}Set branch {Fore.GREEN}{module.branch}{Fore.YELLOW} for {Fore.GREEN}{module.name}")
            # call("git submodule set-branch --branch " + module.branch + " -- " + module.path, shell=True,
            # cwd=currentDir)
            call("git config -f .gitmodules submodule." + module.path + ".branch " + module.branch, shell=True,
                 cwd=currentDir)
            call("git stash", shell=True, cwd=currentDir + "/" + module.path)
            retcode = call("git checkout " + module.branch, shell=True, cwd=currentDir + "/" + module.path)
            if retcode != 0:
                call("git checkout -b " + module.branch, shell=True, cwd=currentDir + "/" + module.path)
            call("git pull", shell=True, cwd=currentDir + "/" + module.path)

# ================================ Checking for updates

current_updated_dependency_in_modules = update_current_rank(modules, module_by_name)

if args.continous_update:
    for module in current_updated_dependency_in_modules:
        for ds in descendants(calculate_reduced_graph(modules), module):
            if module.is_process():
                process_info[module] = {"status": "WAITING"}

    while len(current_updated_dependency_in_modules) > 0:
        wait_for_modules_to_release(process_info, current_updated_dependency_in_modules)
        current_updated_dependency_in_modules = update_current_rank(modules, module_by_name)
        if not args.dirty:
            save_modules(modules, module_by_name)

if args.relnotes:
    currentDir = os.getcwd()
    hashes = args.relnotes[0].split("..")
    if (len(hashes) == 2):
        current_file_name = "project-meta-" + hashes[1] + ".yml"
        call("git show " + hashes[1] + ":project-meta.yml", shell=True, stdout=open(current_file_name, "w"))
    else:
        current_file_name = "project-meta-current.yml"
        shutil.copyfile("project-meta.yml", current_file_name)
    current_modules_by_name = {}
    current_modules = []
    for item in load_modules(current_file_name):
        process_module(item, current_modules, current_modules_by_name)

    previous_file_name = "project-meta-" + hashes[0] + ".yml"
    call("git show " + hashes[0] + ":project-meta.yml", shell=True, stdout=open(previous_file_name, "w"))
    previous_modules_by_name = {}
    previous_modules = []
    for item in load_modules(previous_file_name):
        process_module(item, previous_modules, previous_modules_by_name)
    new_modules = []
    module_versions = {}
    for module in current_modules:
        if module.name in previous_modules_by_name:
            module_versions[module.name] = (module.version, previous_modules_by_name[module.name].version)
        else:
            new_modules.append(module)
    print("New modules:")
    print(new_modules)

    issues = set()

    for module in current_modules:
        if module.name in module_versions:
            if (module.version != module_versions[module.name][1]):
                print(f"Getting logs for {module.name}")
                from_tag = ''
                to_tag = ''
                try:
                    from_tag = module_versions[module.name][1]
                    command = f"git rev-parse -q --verify \"refs/tags/{from_tag}\" >/dev/null"
                    check_output(command, shell=True, cwd=currentDir + "/" + module.path)
                except:
                    try:
                        from_tag = "v" + module_versions[module.name][1]
                        command = f"git rev-parse -q --verify \"refs/tags/{from_tag}\" >/dev/null"
                        check_output(command, shell=True, cwd=currentDir + "/" + module.path)
                    except:
                        print("Version tag: {module_versions[module.name][1]} not found for {module.name}")

                try:
                    to_tag = module_versions[module.name][0]
                    command = f"git rev-parse -q --verify \"refs/tags/{to_tag}\" >/dev/null"
                    check_output(command, shell=True, cwd=currentDir + "/" + module.path)
                except:
                    try:
                        to_tag = "v" + module_versions[module.name][0]
                        command = f"git rev-parse -q --verify \"refs/tags/{to_tag}\" >/dev/null"
                        check_output(command, shell=True, cwd=currentDir + "/" + module.path)
                    except:
                        print("Version tag: {module_versions[module.name][1]} not found for {module.name}")

                if from_tag and to_tag:
                    command = f"git log --pretty=oneline {from_tag}..{to_tag}"
                    log_output = check_output(command, shell=True, cwd=currentDir + "/" + module.path)

                    for line in log_output.splitlines():
                        m = re.search('JNG-\d+', line.decode())
                        if m:
                            issues.add(m.group())

    for module in new_modules:
        command = f"git log --pretty=oneline"
        log_output = check_output(command, shell=True, cwd=currentDir + "/" + module.path)
        for line in log_output.splitlines():
            m = re.search('JNG-\d+', line.decode())
            if m:
                issues.add(m.group())

    now = datetime.now()
    jira = Jira(
        url='https://blackbelt.atlassian.net', username=args.jira_user, password=args.jira_token)

    output = "Versions\n"
    output += "--------\n"
    output += "\n"
    output += "|=======================\n"
    output += f"| JUDO Designer       | {module_by_name['judo-epp-designer'].version}\n"
    output += f"| JUDO Platform       | {module_by_name['judo-platform'].version}\n"
    output += f"| JUDO Tatami         | {module_by_name['judo-tatami'].version}\n"
    output += f"| JUDO Tatami Client  | {module_by_name['judo-tatami-client'].version}\n"
    output += f"| JUDO Services       | {module_by_name['judo-services'].version}\n"
    output += f"| JUDO DAO API        | {module_by_name['judo-dao-api'].version}\n"
    output += f"| JUDO Dispatcher API | {module_by_name['judo-dispatcher-api'].version}\n"
    output += f"| JUDO SDK Common     | {module_by_name['judo-sdk-common'].version}\n"
    output += f"| JUDO RDBMS Schema   | {module_by_name['judo-rdbms-schema'].version}\n"
    output += f"| JUDO Architect      | {module_by_name['judo-epp-architect'].version}\n"
    output += f"|=======================\n"
    output += "\n"
    output += "Download Designer\n"
    output += "-----------------\n"
    output += "\n"
    output += f"Version: {module_by_name['judo-epp-designer'].version}\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-macosx.cocoa.x86_64.tar.gz[MacOS Intel] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-macosx.cocoa.aarch64.tar.gz[MacOS Apple Silicon] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-linux.gtk.x86_64.tar.gz[Linux x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-linux.gtk.aarch64.tar.gz[Linux Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-linux.gtk.x86_64_all.deb[Linux Debian/Ubuntu x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-linux.gtk.aarch64_all.deb[Linux Debian/Ubuntu Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-win32.win32.x86_64.zip[Windows ZIP] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/{module_by_name['judo-epp-designer'].version}/judo-designer_{module_by_name['judo-epp-designer'].version}-win32.win32.x86_64.exe[Windows Installer] |\n"
    output += "\n"
    output += "Download Architect\n"
    output += "------------------\n"
    output += "\n"
    output += f"Version: {module_by_name['judo-epp-architect'].version}\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-macosx.cocoa.x86_64.tar.gz[MacOS Intel] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-macosx.cocoa.aarch64.tar.gz[MacOS Apple Silicon] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-linux.gtk.x86_64.tar.gz[Linux x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-linux.gtk.aarch64.tar.gz[Linux Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-linux.gtk.x86_64_all.deb[Linux Debian/Ubuntu x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-linux.gtk.aarch64_all.deb[Linux Debian/Ubuntu Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-win32.win32.x86_64.zip[Windows ZIP] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/{module_by_name['judo-epp-architect'].version}/judo-architect_{module_by_name['judo-epp-architect'].version}-win32.win32.x86_64.exe[Windows Installer] |\n"
    output += "\n"
    output += "JUDO Eclipse development plugin site\n"
    output += "------------------------------------\n"
    output += f"Version: {module_by_name['judo-eclipse-development'].version}\n"
    output += "\n"
    output += "The JUDO Architect does not contain any JUDO meta models or JQL / JCL plugins. This update site contains all required artifacts which are used to build Eclipse Designer.\n"
    output += f"To install plugins open `Install new Software` window and in update site add `https://nexus.judo.technology/repository/p2/judo-eclipse-development/{module_by_name['judo-eclipse-development'].version}`/ \n"
    output += "To update a previously installed version, set the update site URL to the desired one and update the plugin.\n"
    output += "\n"
    output += "JUDO Modules\n"
    output += "------------\n"
    output += "\n"
    output += "|=======================\n"
    output += "| Name | GitHUB | Version\n"
    for module in modules:
        output += f"| {module.name} | https://github.com/{module.github}[{module.github}] | https://github.com/{module.github}/releases/tag/v{module.version}[{module.version}^]\n"
    output += f"|=======================\n"

    output += "Changelog\n"
    output += "---------\n"
    output += "\n"
    output += "[options=\"header\"]\n"
    output += "\n"
    output += "|=======================\n"
    output += "| ID | Type | Summary | Status | Notes\n"

    # For JIRA API see https://atlassian-python-api.readthedocs.io/jira.html
    for issueNumber in sorted(issues):
        print(f"Querying JIRA for {issueNumber}")
        try:
            note = ""
            try:
                noteSubtasksResult = jira.jql(f"parent = {issueNumber} AND labels = Note", expand="renderedFields")
                for issue in noteSubtasksResult['issues']:
                    description = issue['renderedFields']['description']  # Get HTML formatted content
                    note += f"{description}<br>"
            except BaseException as err:
                print(f"An exception occurred on fetching  subtasks of {issueNumber} - {err=}, {type(err)=}")

            issue = jira.issue(issueNumber, 'summary, status, issuetype')
            issuetype = issue['fields']['issuetype']['name']
            summary = issue['fields']['summary']
            status = issue['fields']['status']['name']
            output += f"| https://blackbelt.atlassian.net/browse/{issueNumber}[{issueNumber}^] | {issuetype} | {summary} | {status} | {note}\n"
        except BaseException as err:
            print(f"An exception occurred on fetching {issueNumber} - {err=}, {type(err)=}")
    output += "|=======================\n"

    relnotes_file = open("relnotes_" + now.strftime("%Y_%m_%d") + ".adoc", "w")
    relnotes_file.write(output)
    relnotes_file.close()
