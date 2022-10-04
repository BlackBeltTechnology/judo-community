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

If there is any problem with dependencies, try:

      pip install -r requirements.txt --upgrade --force-reinstall

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

    ./project.py -bs -c <module_to_continue_from>


To start a build from judo-meta-jsl, type

    ./project.py -bs -sm judo-meta-jsl

It can contain several start module.

If you do not want to build the whole dependency chain, you can define terminate modules.
In this case the modules between start and terminate modules will be built.

    ./project.py -bs -sm judo-meta-jsl -tm judo-runtime-core-jsl


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
import atexit
import base64
import http
import json
import os
import re
import socket
import webbrowser
from argparse import RawDescriptionHelpFormatter, ArgumentParser
from typing import Dict, Any

import git
from github import Github, UnknownObjectException
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

from asciidag.graph import Graph as AsciiGraph
from asciidag.node import Node as AsciiNode
from progressbar import progressbar, ProgressBar
from tqdm import tqdm

import argparse
import textwrap

from http.server import BaseHTTPRequestHandler
import threading

from datetime import datetime

from colorama import init, Fore, Style

from atlassian import Jira

# TODO: Adapt https://hal.archives-ouvertes.fr/hal-00695818/document
# TODO: Adapt https://www.cse.wustl.edu/~lu/papers/tpds-dags.pdf

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
general_arg_group.add_argument("-sm", "--start-modules", action="store", dest="start_modules", default=None,
                               metavar='MODULE...', nargs="*",
                               help='Run only on the defined module(s)')
general_arg_group.add_argument("-tm", "--terminate-modules", dest="terminate_modules",
                               metavar='MODULE...', nargs="*",
                               help="The process is terminated with these given modules. Only the models between "
                                    "modules and terminate modules will processed. ")
general_arg_group.add_argument("-im", "--ignored-modules", dest="ignored_modules", metavar='MODULE...',
                               nargs="*",
                               help="Ignore given module(s)")
general_arg_group.add_argument("-c", "-continue-module", dest="continue_module",
                               metavar='MODULE', nargs=1,
                               help="Continue processing from the given module")
general_arg_group.add_argument("-relnotes", "--release-notes", dest="relnotes", nargs=1,
                               metavar="FROM_HASH[..TO_HASH]",
                               help="Generate release notes based on different project-meta.yml versions")

localbuild_arg_group = parser.add_argument_group('Local build control arguments')
localbuild_arg_group.add_argument("-bs", "--build-snapshot", dest="build_snapshot", action='store_true', default=False,
                                  help="Build modules")
localbuild_arg_group.add_argument("-kc", "--keep-changes", dest="keep_changes", action='store_true', default=False,
                                  help="Keep changes in files")

git_arg_group = parser.add_argument_group('GIT control arguments')
git_arg_group.add_argument("-gc", "--gitcheckout", action="store_true", dest="git_checkout", default=False,
                           help='Fetch / Reset / Checkout branch')
git_arg_group.add_argument("-pu", "--pushupdates", action="store_true", dest="push_updates", default=False,
                           help='Push updates in projects')
git_arg_group.add_argument("-noci", "--noci", action="store_true", dest="ci_skip", default=False,
                           help='Make commit with CI Ignore')

github_arg_group = parser.add_argument_group('GitHub API control arguments')
github_arg_group.add_argument("-fv", "--fetchversions", action="store_true", dest="fetch_github_versions",
                              default=False,
                              help='Fetch last released versions from github')
github_arg_group.add_argument("-cb", "--createfeaturebranch", action="store", dest="create_branch",
                              metavar='Feature name', nargs=1,
                              help='Create feature branch')
github_arg_group.add_argument("-sb", "--switchfeaturebranch", action="store", dest="switch_branch",
                              metavar='Feature name', nargs=1,
                              help='Switch to feature branch')
github_arg_group.add_argument("-pr", "--createpr", action="store", dest="create_pr",
                              metavar='Pull request name', nargs=1,
                              help='Create pull requesr')

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
                              help='Jira token used for authentication '
                                   '(https://id.atlassian.com/manage-profile/security/api-tokens)')
access_arg_group.add_argument("-jusr", "--jirauser", action="store", dest="jira_user",
                              default=os.environ.get('JUDO_JIRA_USER', ''),
                              help='Jira user used for authentication - same user as token user')

args = parser.parse_args()

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

# progress_widgets = [' [',
#           progressbar.Timer(format='elapsed time: %(elapsed)s'),
#           '] ',
#           progressbar.Bar('*'), ' (',
#           progressbar.ETA(), ') ',
#           ]

github = Github(login_or_token=args.github_token)


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
        if not self.is_processable():
            return
        # repository = github.get_organization(par['github'].split("/")[0]).get_repo(par['github'].split("/")[1])
        repository = github.get_repo(self.github)
        if self.branch == 'master':
            for tag in repository.get_tags():
                if tag.name and re.match(r'^v(\d+\.)?(\d+\.)?(\*|\d+)$', tag.name):
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
                if tag.name and re.match(r'^v.*' + self.branch + '.*', tag.name):
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
            return False

        if not self.is_processable():
            return False

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

    def repo(self):
        _currentDir = os.getcwd() + '/' + self.path
        return git.Repo(_currentDir)

    def checkout_branch(self):
        _repo = self.repo()
        print(f"{Fore.YELLOW}Checkout: " + _repo.git_dir)
        if self.check_dirty():
            _repo.git.stash("-m \"[AUTO STASH]\"")
        # _repo.git.checkout(self.branch)
        # _repo.git.pull()

        _repo.git.fetch()
        # Create a new branch
        # repo.git.branch('my_new_branch')
        # You need to check out the branch after creating it if you want to use it
        _repo.git.checkout(self.branch)

    def check_dirty(self):
        _currentDir = os.getcwd() + '/' + self.path
        repo = git.Repo(os.getcwd() + '/' + self.path)
        return repo.is_dirty(untracked_files=True)

    def commit_and_push_changes(self):
        _repo = self.repo()

        print(f"{Fore.YELLOW}Commit and push: " + _repo.git_dir)
        _repo.git.add(all=True)

        _commit_message = "[Release] Updating versions"
        if args.ci_skip:
            _commit_message = _commit_message + " [ci skip]"
        _repo.index.comit(_commit_message)
        _origin = _repo.remote(name='origin')
        _origin.push()
        _repo.git.push()

    def is_processable(self):
        return not self.ignored and self.virtual
        # (not args.module or args.module == self.name) and args.min_rank <= self.rank <= args.max_rank

    def switch_branch(self, _branch_name):
        self.branch = _branch_name
        self.checkout_branch()

    def create_branch(self, _branch_name):
        _repo = github.get_repo(self.github)

        # if self._dirty:
        #     raise SystemExit(f"\n{Fore.RED}Repo have uncommited changes: {self.name}.")

        # _hashes = list(_repo.get_commits())
        # if len(_hashes) <= 1:
        #     raise SystemExit(f"\n{Fore.RED}No commits found on repo: {self.name}.")
        # _start_hash = _hashes[-1].sha

        _develop = _repo.get_branch("develop")

        _branch = None
        try:
            _branch = _repo.get_git_ref("heads/" + _branch_name)
        except UnknownObjectException:
            _branch = _repo.create_git_ref("refs/heads/" + _branch_name, sha=_develop.commit.sha)

        self.branch = _branch_name
        self.checkout_branch()

    def create_pull_request(self, _message):
        _repo = github.get_repo(self.github)
        #_branch = _repo.get_git_ref("heads/" + self.branch)
        pr = _repo.create_pull(
            title=_message,
            body=_message,
            head='refs/heads/' + self.branch,
            base='refs/heads/develop',
            draft=True,
            maintainer_can_modify=True
        )


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
        for _dependency in _module.dependencies:
            if _dependency in _modules:
                _g.add_edge(_module, _dependency)
    return _g


def calculate_reduced_graph(_modules):
    _gt = transitive_reduction(calculate_graph(_modules))
    return _gt


def calculate_ranks(_modules):
    _g = calculate_reduced_graph(_modules).reverse(copy=True)
    _groups = list(topological_sort_grouped(_g))
    rank = 0
    for _group in _groups:
        rank += 1
        for _module in _group:
            _module.rank = rank


# Algorithm from: https://stackoverflow.com/questions/56802797/digraph-parallel-ordering
# Edges: (1, 2) (2, 4) (3, 4), (4, 5), (4, 6), (6, 7)
# In [21]: list(nx.topological_sort(G))
# Out[21]: [3, 1, 2, 4, 6, 7, 5]
#
# In [22]: list(topological_sort_grouped(G))
# Out[22]: [[1, 3], [2], [4], [5, 6], [7]]
def topological_sort_grouped(_g):
    indegree_map = {v: d for v, d in _g.in_degree() if d > 0}
    zero_indegree = [v for v, d in _g.in_degree() if d == 0]
    while zero_indegree:
        yield zero_indegree
        new_zero_indegree = []
        for v in zero_indegree:
            for _, child in _g.edges(v):
                indegree_map[child] -= 1
                if not indegree_map[child]:
                    new_zero_indegree.append(child)
        zero_indegree = new_zero_indegree


def scrub_dict(d):
    new_dict = {}
    for k, v in d.items():
        if isinstance(v, dict):
            v = scrub_dict(v)
        if isinstance(v, list):
            v = scrub_list(v)
        if v not in (u'', None, {}, []):
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
    _g = calculate_reduced_graph(_modules)
    for node in _g.nodes():
        _g.nodes[node]['shape'] = 'box'
        _g.nodes[node]['label'] = f"{node.name} ({node.rank})"
    write_dot(_g, "dependency.dot")
    to_pydot(_g).write_svg("dependency.svg")


def print_dependency_graph_ascii(_modules):
    _available_modules = set(filter(lambda _m: not _m.ignored and not _m.virtual, _modules))

    _g = calculate_reduced_graph(_available_modules)
    _groups = list(topological_sort_grouped(_g))

    _graph_root = []
    _graph_nodes = {}

    _prev_group = []
    for _group in list(reversed(_groups)):
        for _module in _group:
            _ancestors = set(ancestors(_g, _module))
            _descendants = set(descendants(_g, _module)).intersection(_prev_group)
            _parent_nodes = list(map(lambda _d: _graph_nodes[_d.name], _descendants))
            _current_node = AsciiNode(str(_module), _parent_nodes)
            _graph_nodes[_module.name] = _current_node
            if len(_ancestors) == 0:
                _graph_root.append(_current_node)

        _prev_group = _group

    _graph = AsciiGraph()
    _graph.show_nodes(_graph_root)


def get_request_handler(_modules, _process_info):
    class MyHandler(http.server.BaseHTTPRequestHandler):

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

        def do_GET(self):

            _g = nx.DiGraph()
            for _module in _process_info.keys():
                _g.add_node(_module)

            for _module in _modules:
                for dependency in _module.dependencies:
                    if dependency in _modules:
                        _g.add_edge(_module, dependency)

            _g = transitive_reduction(_g)
            for node in _g.nodes():
                _g.nodes[node]['shape'] = 'box'
                _g.nodes[node]['label'] = f"{node.name} ({node.rank})"
                _g.nodes[node]['fillcolor'] = 'azure3'
                _g.nodes[node]['style'] = 'filled'
                if _process_info.get(node, {"status": "UNKNOWN"}).get("status") == "UNKNOWN":
                    _g.nodes[node]['fillcolor'] = 'wheat'
                if _process_info.get(node, {"status": ""}).get("status") == "RUNNING":
                    _g.nodes[node]['fillcolor'] = "yellow"
                if _process_info.get(node, {"status": ""}).get("status") == "OK":
                    _g.nodes[node]['fillcolor'] = "green"

            svg = to_pydot(_g).create_svg().decode("utf-8")

            message_bytes = svg.encode('ascii')
            base64_bytes = base64.b64encode(message_bytes)
            body = "<html xmlns=\"http://www.w3.org/1999/xhtml\"><head><title/></head><body onload=\"load()\">" \
                   "<script type=\"text/javascript\">" \
                   "function load() { setTimeout(\"window.open(self.location, '_self');\", 5000); }" \
                   "</script>" \
                   "<embed src=\"data:image/svg+xml;base64," + base64_bytes.decode('ascii') + "\"/>" + "</body></html>"

            self.send_response(200)
            self.send_header("Content-type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(bytes(body, "utf-8"))

    return MyHandler


def start_process_info_server(_modules, _process_info):
    _port = 8000
    while is_port_in_use(_port):
        _port += 1

    print(f"{Fore.YELLOW}Serving at port:{Fore.GREEN}{_port}")
    server_address = ('localhost', _port)
    httpd: StoppableHTTPServer = StoppableHTTPServer(server_address, get_request_handler(_modules, _process_info))
    threading.Thread(target=lambda: webbrowser.open_new_tab(f"http://127.0.0.1:{_port}")).start()
    return httpd


def check_modules_for_update(_modules, _module_by_name):
    # Check implications on modules
    _updated_modules = []
    for _module in _modules:
        if _module.is_processable():
            # print("Checking dependency update in POM: " + module.name)
            if _module.update_dependency_versions_in_pom(False):
                _updated_modules.append(_module)

    _current_updated_dependency_in_modules = _updated_modules

    # Removing modules which have dependency on current modules
    for _module in _updated_modules:
        _current_updated_dependency_in_modules = _current_updated_dependency_in_modules.difference(
            set(ancestors(calculate_reduced_graph(_modules), _module)))

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

def fetch_github_versions(_modules):
    for _module in _modules:
        #        if _module.is_processable():
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
    print(
        f"\n{Fore.GREEN}================================================================================"
        f"{Style.RESET_ALL}")
    print(f"\n{Fore.YELLOW}Building module{Style.RESET_ALL} {Fore.WHITE}{_module.path}{Style.RESET_ALL}")
    _currentDir = os.getcwd() + '/' + _module.path
    if not _module.call_beforelocalbuildscripts():
        return False

    _original_versions = {}

    _pom = parse(open(_module.path + "/pom.xml"), parser=XMLParser(target=CommentedTreeBuilder()))
    _root = _pom.getroot()
    _properties = _root.find(pom_namespace + 'properties')

    # Update versions properties to snapshot in pom.xml
    for arg in arguments:
        _versionName = arg.split("=")[0]
        if "-version" in _versionName:
            _version = arg.split("=")[1]
            _ref_prop_element = _properties.find(pom_namespace + _versionName)
            if _ref_prop_element is not None:
                _original_versions[_versionName] = _ref_prop_element.text
                _ref_prop_element.text = _version

    if len(_original_versions) > 0:
        _pom.write(_module.path + "/pom.xml", encoding="UTF-8")

    cmd = "mvn clean install  " + (" ".join(list(map(lambda x: f"-D{x}", arguments))))
    print(f"{Fore.YELLOW}Calling maven: \n\t{Fore.CYAN}{cmd}{Style.RESET_ALL}\n")
    print(
        f"{Fore.GREEN}================================================================================"
        f"{Style.RESET_ALL}")
    _ret = call(cmd, shell=True, cwd=_currentDir)

    if not args.keep_changes:
        # Restore versions properties to original values in pom.xml
        if len(_original_versions) > 0:
            for _versionName in _original_versions.keys():
                _ref_prop_element = _properties.find(pom_namespace + _versionName)
                _ref_prop_element.text = _original_versions[_versionName]
            _pom.write(_module.path + "/pom.xml", encoding="UTF-8")

    if _ret != 0:
        return False
    return True


def get_module(_module_names, _module_by_name):
    _modules = set()
    if _module_names is not None:
        for _module_name in _module_names:
            _module = _module_by_name[_module_name]
            if _module is None:
                raise SystemExit(f"\n{Fore.RED}No module name is found {_module_name}.")
            if _module.virtual:
                raise SystemExit(f"\n{Fore.RED}Virtual module cannot be used in snapshot mode: {_module_name}.")
            _modules.add(_module)
    return _modules


def terminate_modules(_module_by_name):
    return get_module(args.terminate_modules, _module_by_name)


def ignored_modules(_module_by_name):
    return get_module(args.ignored_modules, _module_by_name)


def start_modules(_module_by_name):
    return get_module(args.start_modules, _module_by_name)


def calc_retry_command_for_build(_modules, _process_info, _module_by_name):
    _retry_command = ""

    _start_modules = start_modules(_module_by_name)
    if len(_start_modules) > 0:
        _retry_command = _retry_command + "-sm "
        for _module in _start_modules:
            _retry_command = _retry_command + _module.name + " "

    _ignored_modules = ignored_modules(_module_by_name)
    if len(_ignored_modules) > 0:
        _retry_command = _retry_command + "-im "
        for _module in _ignored_modules:
            _retry_command = _retry_command + _module.name + " "

    _terminate_modules = terminate_modules(_module_by_name)
    if len(_terminate_modules) > 0:
        _retry_command = _retry_command + "-tm "
        for _module in _terminate_modules:
            _retry_command = _retry_command + _module.name + " "

    return _retry_command


def calculate_processable_modules(_modules, _process_info, _module_by_name):
    _available_modules = set(filter(lambda _m: not _m.ignored and not _m.virtual, _modules))
    _ignored_modules = ignored_modules(_module_by_name).union(
        set(filter(lambda _m: _m.virtual or _m.ignored, _modules)))
    _start_modules = start_modules(_module_by_name)
    _modules_to_build = set(_available_modules)
    _terminate_modules = terminate_modules(_module_by_name)

    # if len(_start_modules) > 0:
    #     print(f"{Fore.YELLOW}Start modules:{Style.RESET_ALL} " + (
    #         ", ".join(map(lambda _m: _m.name, list(_start_modules)))))

    if len(_start_modules) == 0:
        _g = calculate_graph(_available_modules).reverse(copy=True)
        _groups = list(topological_sort_grouped(_g))
        if len(_groups) > 0:
            _start_modules = _groups[0]

    if len(_terminate_modules) > 0:
        for _module_terminate in _terminate_modules:
            # removing terminate modules ancestors
            _terminate_ancestors = set(ancestors(calculate_graph(_available_modules), _module_terminate))
            _terminate_descendants = set(
                descendants(calculate_graph(_available_modules), _module_terminate))

            # print(f"{Fore.GREEN}{_module_terminate.name} {Style.RESET_ALL} terminate ancestors: " + (
            #     ", ".join(
            #         map(lambda _m: _m.name, _terminate_ancestors))))
            #
            # print(f"{Fore.GREEN}{_module_terminate.name} {Style.RESET_ALL} terminate descendants: " + (
            #     ", ".join(
            #         map(lambda _m: _m.name, _terminate_descendants))))

    if len(_start_modules) > 0:
        _modules_to_build = set(_start_modules)
        _start_modules_ancestors = set()
        for _module in _start_modules:
            _start_modules_ancestors = _start_modules_ancestors.union(set(ancestors(calculate_graph(_available_modules),
                                                                                    _module)))
            # print(f"{Fore.BLUE}{_module.name} {Style.RESET_ALL} ancestors (all): " + (
            #     ", ".join(
            #         map(lambda _m: _m.name, _calculated_modules))))

            _terminate_ancestors = set()
            _terminate_descendants = set()

            for _module_terminate in _terminate_modules:
                # removing terminate modules ancestors
                _terminate_ancestors = _terminate_ancestors.union(set(ancestors(
                    calculate_graph(_available_modules), _module_terminate)))
                _terminate_descendants = _terminate_descendants.union(set(descendants(
                    calculate_graph(_available_modules), _module_terminate)))
                # the required modules is the intersection of terminate modules's descendanes and module's ascendents

        _calculated_modules = _start_modules_ancestors
        if len(_terminate_descendants) > 0 or len(_terminate_ancestors) > 0:
            _calculated_modules = _start_modules_ancestors.union(_start_modules) \
                .intersection(_terminate_descendants.union(_terminate_modules))

            # print(f"{Fore.BLUE}{_module.name} {Style.RESET_ALL} ancestors (reduced with terminated): " + (
            #     ", ".join(
            #         map(lambda _m: _m.name, _calculated_modules))))
        _modules_to_build = _modules_to_build.union(_calculated_modules)

    _modules_to_build = sorted(_modules_to_build.difference(_ignored_modules), key=lambda _m: (_m.rank, _m.name))
    _all_modules = sorted(_modules, key=lambda _m: (_m.rank, _m.name))

    print(f"\n{Fore.YELLOW}Modules to process {Fore.GREEN}✓ - process {Fore.RED}✘ - ignored{Style.RESET_ALL}:\n" + (
        f"\n".join(map(lambda _m: (f"{Fore.RED}✘  {Style.RESET_ALL}" if _m in _ignored_modules
                                   else (f"{Fore.GREEN}✓  {Style.RESET_ALL}" if _m in _modules_to_build
                                         else "   ")) + f"{Fore.WHITE}{_m.name} ({_m.rank}){Style.RESET_ALL}",
                       _all_modules))) + f"{Style.RESET_ALL}\n")

    return _modules_to_build


def build_snapshot(_modules, _process_info, _module_by_name):
    _ignored_modules = ignored_modules(_module_by_name)

    #    if len(ignored_modules()) > 0:
    #        print(f"{Fore.YELLOW}Modules ignored:{Style.RESET_ALL} " + (
    #            ", ".join(map(lambda _m: _m.name, list(_ignored_modules)))) + "\n")
    #    ✓

    _retry_command = './project.py -bs ' + calc_retry_command_for_build(_modules, _process_info, _module_by_name)
    _version_args = []
    for _module in _modules:
        _version_args.append(_module.property + "=" + current_snapshot_version(_module))

    print(f"{Fore.YELLOW}Versions override:{Style.RESET_ALL}\n\t" + f"\n\t".join(
        map(lambda _v: f"{Fore.WHITE}{_v.split('=')[0]} {Fore.YELLOW}={Fore.GREEN} {_v.split('=')[1]}{Style.RESET_ALL}",
            _version_args)))

    if args.continue_module:
        _start_module_name = args.continue_module[0]
        _build_from_index = list(map(lambda _m: _m.name, _modules)).index(_start_module_name)
        _modules = list(_modules[_build_from_index:])

    for _module in _modules:
        _process_info[_module] = {"status": "RUNNING"}

        if _module.p2 and 'target' in _module.p2.keys():
            if 'releaselocations' not in _module.p2.keys():
                raise SystemExit(f"\n{Fore.RED}Error when building {_module.name}. `releaselocations` not defined. "
                                 f"\nTo retry, run {Fore.YELLOW}{_retry_command} -c {_module.name}{Style.RESET_ALL}")

            with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['releaselocations'])) as f:
                _releaseLocs = {k: v for _line in filter(str.rstrip, f) for (k, v) in [_line.strip().split(None, 1)]}

            _locations: dict[Any, str] = dict(_releaseLocs)
            for _dependency_module in _module.dependencies:
                _locations_key = _dependency_module.name + "-location"
                _version = current_pom_version(_module, _dependency_module)

                if not _version:
                    raise SystemExit(
                        f"\n{Fore.RED}Error when building {_module.name}. "
                        f"Dependency {_dependency_module.name} version not found. "
                        f"\nTo retry, run {Fore.YELLOW}{_retry_command} -c {_module.name}{Style.RESET_ALL}")
                _value = None

                if _dependency_module in _modules and _dependency_module.p2:
                    _value = "file:" + os.path.abspath(
                        os.getcwd() + "/" + _dependency_module.path + "/" + _dependency_module.p2['localsite'])
                elif _locations_key in _releaseLocs.keys():
                    _value = _releaseLocs[_locations_key]

                if _value:
                    _locations[_locations_key] = _value.replace("${" + _dependency_module.property + "}", _version)

            print(f"\n{Fore.YELLOW}P2 Locations: {Style.RESET_ALL}\n {json.dumps(_locations, indent=4)}")

            with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['template'])) as _tf:
                _template = "".join(_tf.readlines()).replace("${build.timestamp.millis}", str(int(time.time() * 1000)))
                for _loc in _locations.keys():
                    _template = _template.replace("${" + _loc + "}", _locations[_loc])
                with open(os.path.abspath(os.getcwd() + "/" + _module.path + "/" + _module.p2['target']), 'w',
                          encoding='utf-8') as _out:
                    _out.write(_template)

        _status = build_module(_module, _version_args)
        _status2 = True

        if not args.keep_changes:
            # Usually restore .target file
            _module.call_afterlocalbuildscripts()

        if not _status:
            raise SystemExit(f"\n{Fore.RED}Error when building {_module.name}. "
                             f"\nTo retry, run {Fore.YELLOW}{_retry_command} -c {_module.name}{Style.RESET_ALL}")
        _process_info[_module] = {"status": "OK"}


def build_continuous(_modules, _process_info, _module_by_name):
    _modules_to_process = check_modules_for_update(_modules, _module_by_name)
    for _module in _modules:
        _process_info[module] = {"status": "WAITING"}

    while len(_modules_to_process) > 0:
        # wait_for_modules_to_release(process_info, current_updated_dependency_in_modules)
        time.sleep(15)
        _recalculate_process_modules = False
        for _module in _modules_to_process:
            _process_info[_module] = {"status": "RUNNING"}
            print(
                f"{Fore.YELLOW}Checking latest release for {Fore.GREEN}{_module.name}{Fore.YELLOW} in branch: "
                f"{Fore.GREEN}{_module.branch}")
            if _module.update_github_versions():
                print("  NEW Version, it removed from wait list")
                _process_info[_module] = {"status": "OK"}
                _recalculate_process_modules = True

        if _recalculate_process_modules:
            _new_modules_to_process = check_modules_for_update(modules, _module_by_name)
            # TODO: Finish calculation - have to remove modules which have anchestors between
            # for _processing_module in _new_modules_to_process:
            #    set(descendants(calculate_reduced_graph(_modules), _processing_module))

        if not args.dirty:
            save_modules(modules, _module_by_name)


def is_port_in_use(_port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", _port)) == 0


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
    print(f"{Fore.YELLOW}Stop server\n{Style.RESET_ALL}")


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

_processable_modules = calculate_processable_modules(modules, process_info, module_by_name)

print_dependency_graph_ascii(_processable_modules)

# =============================== Fetch / Checkout / Reset Git
if args.git_checkout:
    _currentDir = os.getcwd()
    _repo = git.Repo(_currentDir)
    # for _submodule in _repo.submodules:
    #    print(f"{Fore.YELLOW}Update submodule {Fore.GREEN}{_submodule.name}{Style.RESET_ALL}")
    #    _submodule.update(init=True)

    _modules = tqdm(_processable_modules)
    for _module in _modules:
        if not module.virtual:
            _modules.set_description(_module.name)
            # print(f"{Fore.YELLOW}Update submodule {Fore.GREEN}{_module.name}{Style.RESET_ALL}")
            _repo.submodule(_module.path).update(init=True)
            # print(f"{Fore.YELLOW}Set branch {Fore.GREEN}{_module.branch}{Fore.YELLOW} for {Fore.GREEN}{_module.name}{Style.RESET_ALL}")
            module.checkout_branch()

if args.fetch_github_versions:
    fetch_github_versions(modules)

if args.create_branch:
    for _module in _processable_modules:
        if not _module.virtual and not _module.ignored:
            _module.create_branch(args.create_branch[0])

if args.switch_branch:
    for _module in _processable_modules:
        if not _module.virtual and not _module.ignored:
            _module.switch_branch(args.switch_branch[0])

if args.create_pr:
    for _module in _processable_modules:
        if not _module.virtual and not _module.ignored:
            _module.create_pull_request(args.create_pr[0])

if args.update_module_pom:
    for module in _processable_modules:
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
    # =============================== Start process info server
    atexit.register(server_shutdown)
    server_start(_processable_modules, process_info)
    build_snapshot(_processable_modules, process_info, module_by_name)

# =============================== Generating Graphviz
if args.graphviz:
    print_dependency_graph(modules)

# =============================== Force postchange run
if args.run_postchangescripts:
    for module in _processable_modules:
        if module.is_processable():
            if not module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {module.name}.")
                if not args.dirty:
                    save_modules(modules, module_by_name)
                exit(1)
            if args.push_updates:
                module.commit_and_push_changes()

# ================================ Checking for updates

if args.continous_update:
    build_continuous(_processable_modules, process_info, module_by_name)

if args.relnotes:
    currentDir = os.getcwd()
    hashes = args.relnotes[0].split("..")
    if len(hashes) == 2:
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
