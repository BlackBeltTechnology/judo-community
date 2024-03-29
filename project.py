#!/usr/bin/env python3

"""
This script provides useful utilities for working with JUDO project source code.
To install make sure you are in the base JUDO project directory.

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

   After installation, it is activated, nothing to do.

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

== Project dependencies

The project dependencies defined as a DAG (Directed Acyclic Graph)
With this tool operations can be performed in the whole graph and some part of it.
When you want to define nodes where the processing start of, use -sm switch,
and define what is the target project with the -tm switches. The logic
will calculate all projects between.

== Update project-meta.yml to contain the latest version in the remote repository

    ./project.py -fv -gh <YOUR_GITHUB_TOKEN_HERE>

The -fv option will check the latest release of all the modules and update the project-meta.yml file accordingly.
This information then can be used to update pom.xml files to use the latest versions of the modules.


== Create branches for projects

   ./project.py -sm judo-meta-psm -tm judo-platform -nf feature/JNG-3834_TestCI "JNG-3834 Test CI capability"

   It creates feature branch, create an empty commit and create draft pull request for all projects between
   judo-meta-psm and judo-platform

== Perform continues build

   ./project.py -sm judo-meta-psm judo-meta-jql judo-meta-expression -tm judo-platform -bs

   It fetches versions for the given modules, updating pom.xml, pushing and waiting for new versions. It will
   traverse graph and orchestrating that all descendants have correct and consistent version.

== Switch branch

    ./project.py -sm judo-meta-psm judo-meta-jql judo-meta-expression -tm judo-platform -sb develop

    Switch branches back to develop for the given modules.

== Execute build locally with module and modules depending on it recursively to snapshot version

Calling the script with the -bs option will execute a local build with SNAPSHOT version. The other modules
will use the versions is defined in their pom.xml

    ./project.py -bs

It starts a build for all modules which is not virtual or ignored by default.
With the -bs switch the modules where the build starts from can be defined. In that case
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

== Change the dependencies of a given module to the latest versions
Assuming you've already updated the versions in the project-meta.yml file, you can update one module's dependencies to
the latest versions:

    ./project.py -ump <module name>

You can do the fetching and the updating in one step:

    ./project.py -fv -ump <module_name> -gh <github_token>


== Releasing using the script
Always release from a separate local repository, not your working copy.

For example clone again:

    git clone --recurse-submodules git@github.com:BlackBeltTechnology/judo-ng.git ~/rel-judo-ng

Make sure everything locally is up-to-date:

    git submodule init
    git submodule update --recursive
    ./project.py -fv -gc -gh $(cat ~/githubtoken)

Switch to the correct tatami branch:

    cd runtime/judo-tatami
    git checkout develop
    cd ../..

Start the builds:

    ./project.py -ib -gh  $(cat ~/githubtoken)

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
import traceback
import sys
import shutil

from argparse import RawDescriptionHelpFormatter, ArgumentParser
from typing import Any, Dict

import git
from github import Github, UnknownObjectException
import yaml
from xml.etree.ElementTree import Comment, register_namespace, parse, XMLParser, TreeBuilder
from subprocess import call

import time
from git import RemoteProgress

import networkx as nx
from networkx.drawing.nx_pydot import write_dot, to_pydot
from networkx.algorithms.dag import transitive_reduction
from networkx.algorithms.dag import ancestors, descendants

from tqdm import tqdm

import argparse
import textwrap

from http.server import BaseHTTPRequestHandler
import threading
import tempfile

from datetime import datetime

from colorama import init, Fore, Style

from atlassian import Jira
import hashlib
from datetime import datetime, timezone
from email.utils import format_datetime
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
general_arg_group.add_argument("-ib", "--integration_build", action="store_true", dest="integration_build",
                               default=False,
                               help='Continuous integration build. (same as -fv -pu -up -cu)')
general_arg_group.add_argument("-rb", "--release_build", action="store_true", dest="release_build",
                               default=False,
                               help='Continuous integration build. (same as -fv -pu -up -cu)')
general_arg_group.add_argument("-fd", "--fix-dependencies", action="store_true", dest="fix_dependencies",
                               default=False,
                               help='Add pom.xml defined dependencies to project-meta.yml')
general_arg_group.add_argument("-cu", "--continuous", action="store_true", dest="continuous_update", default=False,
                               help='Continuously update / fetch / wait until last level')
general_arg_group.add_argument("-sm", "--start-modules", action="store", dest="start_modules", default=None,
                               metavar='MODULE...', nargs="*",
                               help='Run only on the defined module(s)')
general_arg_group.add_argument("-tm", "--terminate-modules", dest="terminate_modules",
                               metavar='MODULE...', nargs="*",
                               help="The process is terminated with these given modules. Only the models between "
                                    "modules and terminate modules will processed. ")
general_arg_group.add_argument("-bm", "--branch-modules", action="store", dest="modules_with_branch", default=None,
                               metavar='Branch name', nargs=1,
                               help='Run only on the module(s) in defined branch')
general_arg_group.add_argument("-im", "--ignored-modules", dest="ignored_modules", metavar='MODULE...',
                               nargs="*",
                               help="Ignore given module(s)")
general_arg_group.add_argument("-c", "-continue-module", dest="continue_module",
                               metavar='MODULE', nargs=1,
                               help="Continue processing from the given module")
general_arg_group.add_argument("-p", "-parallel", type=int, dest="parallel",
                               metavar='MODULE',
                               help="Number of parallel threads", default=4)
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
github_arg_group.add_argument("-fv", "--fetchversions", action="store_true", dest="fetch_versions",
                              default=False,
                              help='Fetch last released versions from github')
github_arg_group.add_argument("-fva", "--fetchversions_all", action="store_true", dest="fetch_versions_all",
                              default=False,
                              help='Fetch last released versions from github for all modules')
github_arg_group.add_argument("-nf", "--newfeature", action="store", dest="new_feature",
                              metavar='branch message', nargs='+',
                              help='Create feature branch and pull request. (same as -cbr and -cpr). '
                                   'If branch name contains spaces, it will be replaced with underscores ("_"). '
                                   'If message is left empty, original, passed branch parameter will be used as '
                                   'PR title. '
                                   'If message/or branch name start with for example feature/ then it will be removed '
                                   'for the PR\'s title and body.')
github_arg_group.add_argument("-ub", "--updatebranch", action="store_true", dest="update_branch",
                              default=False,
                              help='Update checked out branches in project-meta.yml')
github_arg_group.add_argument("-cbr", "--createbranch", action="store", dest="create_branch",
                              metavar='Feature name', nargs=1,
                              help='Create branch')
github_arg_group.add_argument("-sbr", "--switchbranch", action="store", dest="switch_branch",
                              metavar='Feature name', nargs=1,
                              help='Switch to branch')
github_arg_group.add_argument("-cpr", "--createpr", action="store", dest="create_pr",
                              metavar='Pull request name', nargs=1,
                              help='Create pull request')

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


process_info_server: StoppableHTTPServer
process_info_server_pid: threading.Thread

register_namespace('', 'http://maven.apache.org/POM/4.0.0')
pom_namespace = "{http://maven.apache.org/POM/4.0.0}"

github = Github(login_or_token=args.github_token)

class CloneProgress(RemoteProgress):
    def __init__(self):
        super().__init__()

    def update(self, op_code, cur_count, max_count=None, message=''):
        return


class CommentedTreeBuilder(TreeBuilder):
    def __init__(self, *arguments, **kwargs):
        super(CommentedTreeBuilder, self).__init__(*arguments, **kwargs)

    def comment(self, data):
        self.start(Comment, {})
        self.data(data)
        self.end(Comment)

class PropagatingThread(threading.Thread):
    def run(self):
        self.exc = None
        try:
            if hasattr(self, '_Thread__target'):
                # Thread uses name mangling prior to Python 3.
                self.ret = self._Thread__target(*self._Thread__args, **self._Thread__kwargs)
            else:
                self.ret = self._target(*self._args, **self._kwargs)
        except BaseException as e:
            self.exc = e

    def join(self, timeout=None):
        super(PropagatingThread, self).join(timeout)
        if self.exc:
            raise self.exc
        # return self.ret

class Module(object):
    def __init__(self, init_dict):
        self.name = init_dict['name']
        if 'url' in init_dict:
	        self.url = init_dict['url']

        self.path = None
        if 'path' in init_dict:
            self.path = init_dict['path']

        self.branch = init_dict['branch']
        self.property = init_dict['property']
        self.rank = 1
        if 'version' in init_dict:
            self.version = init_dict['version']  # .encode("ascii", "ignore")
        if 'github' in init_dict:
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

    def get_version_from_branch_and_tag(self, _tag):
        _ver = None
        if self.branch == 'master':
            if _tag and re.match(r'^v(\d+\.)?(\d+\.)?(\*|\d+)$', _tag):
                _ver = _tag.strip()[1:]  # .encode('ascii', 'ignore')
        else:
            _branch = re.sub(r"[ #,\\\"'/;-]", "_", self.branch)
            if _tag and re.match(r'^v.*' + _branch + '.*', _tag):
                _ver = _tag.strip()[1:]  # .encode('ascii', 'ignore')
        return _ver

    def update_version_with_given_version(self, _ver):
        if _ver and self.version != _ver:
            print(
                f"{Fore.YELLOW}Updating release version of {Fore.GREEN}{self.name}{Fore.YELLOW}: "
                f"{Fore.GREEN}{self.version} {Fore.YELLOW}=>{Fore.GREEN} {_ver}")
            # if version.parse(ver) < version.parse(self.version):
            #     raise SystemExit(
            #         f"{Fore.RED}{version.parse(ver)} in properties smaller than {version.parse(self.version)} on
            #         project-meta.yml: "
            #         f"{self.name}")
            self.version = _ver
            return True

        return False

    def fetch_github_versions(self):
        if self.ignored:
            return False
        # repository = github.get_organization(par['github'].split("/")[0]).get_repo(par['github'].split("/")[1])
        repository = github.get_repo(self.github)
        for _tag in repository.get_tags():
            _ver = self.get_version_from_branch_and_tag(_tag.name)
            # print(f"Checking tag: {_tag.name} - Ver: {_ver}")
            if self.update_version_with_given_version(_ver):
                return True
            if _ver:
                return False
        return False

    def fetch_git_tag_versions(self):
        if self.ignored:
            return False
        _tags = reversed(sorted(self.get_remote_tags().keys()))
        for _tag in _tags:
            _ver = self.get_version_from_branch_and_tag(_tag)
            # print(f"Checking tag: {_tag} - Ver: {_ver}")
            if _ver and self.version != _ver:
                if self.update_version_with_given_version(_ver):
                    return True
                if _ver:
                    return False
            return False

    def update_dependency_versions_in_pom(self, write_pom=False):
        if self.path is None:
            return False

        if self.ignored or self.virtual:
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
                # if version.parse(dependency.version) < version.parse(ref_prop_element.text):
                #     raise SystemExit(f"{Fore.RED}{version.parse(dependency.version)} in properties smaller than
                #     {version.parse(ref_prop_element.text)} on pom.xml: "
                #                      f"{self.name}")
                print(f" ---> Dependency update: {dependency.name} {ref_prop_element.text} -> {dependency.version}")

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
            _log = tempfile.NamedTemporaryFile()
            _ref = None
            with open(_log.name, 'w') as f:
                _ret = call(afterlocalbuildscript, shell=True, cwd=_currentDir, stdout=f, stderr=f)

            if _ret != 0:
                with open(_log, "r") as f:
                    shutil.copyfileobj(f, sys.stdout)
                return False
        return True

    def call_beforelocalbuildscripts(self):
        _currentDir = os.getcwd() + '/' + self.path
        for beforelocalbuildscript in self.beforelocalbuild:
            _log = tempfile.NamedTemporaryFile()
            _ref = None
            with open(_log.name, 'w') as f:
                _ret = call(beforelocalbuildscript, shell=True, cwd=_currentDir, stdout=f, stderr=f)
            if _ret != 0:
                with open(_log, "r") as f:
                    shutil.copyfileobj(f, sys.stdout)
                return False
        return True

    def repo(self):
        _currentDir = os.getcwd() + '/' + self.path
        return git.Repo(_currentDir)

    def checkout_branch(self):
        _repo = self.repo()
        # print(f"{Fore.YELLOW}Checkout branch {Fore.GREEN}{self.branch} {Fore.YELLOW}in {Fore.GREEN}{_repo.git_dir}")
        if self.check_dirty():
            print(f"{Fore.YELLOW} {self.name } is in dirty state, stashing")
            _repo.git.stash(["-u", "-m \"[AUTO STASH]\""])

        _updates = _repo.git.fetch(["--force", "origin"])
        _updates = _repo.remotes.origin.fetch(progress=CloneProgress())
        # for fetch_info in _updates:
        #     print(f"Tag: {fetch_info.ref} Author: {fetch_info.ref.commit.author} SHA: {fetch_info.ref.commit.hexsha}")

        _repo.git.checkout(self.branch)
        _repo.head.reset(index=True, working_tree=True)
        _repo.remotes.origin.pull(progress=CloneProgress())

    def checkout_tags(self):
        _repo = self.repo()
        _repo.git.fetch(["--tags", "--force", "origin"])

    def get_remote_tags(self):
        _repo = self.repo()
        remote_refs = {}
        for ref in _repo.git.ls_remote("--tags").split('\n'):
            if not ref.startswith("From "):
                hash_ref_list = ref.split('\t')
                if len(hash_ref_list) == 2:
                    if hash_ref_list[1].startswith("refs/tags/"):
                        remote_refs[hash_ref_list[1].removeprefix("refs/tags/")] = hash_ref_list[0]
        return remote_refs

    def check_dirty(self):
        _currentDir = os.getcwd() + '/' + self.path
        _repo = git.Repo(os.getcwd() + '/' + self.path)
        return _repo.is_dirty(untracked_files=True)

    def commit_and_push_changes(self):
        if self.branch == 'master':
            print(f"{Fore.RED} Commit to 'master' branch not allowed for module {self.name}")
            raise SystemExit(1)
        _repo = self.repo()

        print(f"{Fore.YELLOW}Commit and push: " + _repo.git_dir)
        _repo.git.add(all=True)

        prefix = ""
        search = re.search("(JNG-\\d+)", self.branch)
        if search:
            prefix = search.group(0) + " "
        _commit_message = f"{prefix}[Release] Updating versions"
        if args.ci_skip:
            _commit_message = _commit_message + " [ci skip]"
        _repo.index.commit(_commit_message)
        _origin = _repo.remote(name='origin')
        _origin.push()
        _repo.git.push()

    def create_and_push_empty_commit(self, _commit_message):
        if self.branch == 'master':
            print(f"{Fore.RED} Commit to 'master' branch not allowed for module {self.name}")
            raise SystemExit(1)

        _repo = self.repo()

        print(f"{Fore.YELLOW}Create empty commit and push: " + _repo.git_dir)
        _repo.git.add(all=True)

        _repo.index.commit(_commit_message)
        _origin = _repo.remote(name='origin')
        _origin.push()
        _repo.git.push()

    def switch_branch(self, _branch_name):
        self.branch = _branch_name
        if not self.virtual:
            self.checkout_branch()
            self.checkout_tags()

    def create_branch(self, _branch_name):
        _repo = github.get_repo(self.github)

        if " " in _branch_name:
            print(
                f"{Fore.YELLOW}Sanitizing branch name: {Fore.GREEN}{_branch_name} {Fore.YELLOW}=> {Fore.GREEN}"
                f"{_branch_name.replace(' ', '_')}")
            _branch_name = _branch_name.replace(" ", "_")

        found = False
        for branch in _repo.get_branches():
            if str.endswith(branch.name, _branch_name):
                found = True
                break
        if found:
            print(f"{Fore.BLUE}Branch already exists with name '{_branch_name}'")
            self.switch_branch(_branch_name)
            return

        # if self._dirty:
        #     raise SystemExit(f"\n{Fore.RED}Repo have uncommitted changes: {self.name}.")

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
        self.checkout_tags()
        prefix = ""
        search = re.search("(JNG-\\d+)", _branch_name)
        if search:
            prefix = search.group(0) + " "
        self.create_and_push_empty_commit(f"{prefix}Initial feature commit [ci skip]")

    def update_branch_from_git(self):
        # get current branch
        _repo = self.repo()
        self.branch = _repo.active_branch.name

    def create_pull_request(self, _message):
        _repo = github.get_repo(self.github)
        # _branch = _repo.get_git_ref("heads/" + self.branch)

        match = re.match("\\w+/(JNG-\\d+.*)", _message)
        if match:
            _message = match.group(1)

        try:
            print(
                f"{Fore.YELLOW}Creating pull request in {Fore.GREEN}{self.name} {Fore.YELLOW}on branch {Fore.GREEN}"
                f"{self.branch}")
            _repo.create_pull(
                title=_message,
                body=_message,
                head='refs/heads/' + self.branch,
                base='refs/heads/develop',
                draft=True,
                maintainer_can_modify=True
            )
        except Exception as e:
            print(f"{Fore.RED}Creating pull request in {self.name} on branch {self.branch} failed: {e}")

    def perform_release(self):
        # Check all dependency is in master branch
        for _dep in self.dependencies:
            if _dep.branch != "master":
                print(f"{Fore.RED} Error in {self.name} - Dependency: {_dep.name} not in master branch, "
                      f"instead of: {_dep.branch}")
                raise SystemExit(1)

        print(f"[RELEASE] {Fore.YELLOW}{self.name}: Create 'perform-release-on-{self.version}'")
        _tag = self.repo().create_tag("perform-release-on-" + self.version, message="[RELEASE] Perform release")
        self.repo().remotes.origin.push(_tag)
        print(f"[RELEASE]{Fore.YELLOW}{self.name}: Switch branch to 'master'")
        self.switch_branch("master")
        print(f"[RELEASE]{Fore.YELLOW}{self.name}: Checkout 'master' branch")
        self.checkout_branch()
        print(f"[RELEASE]{Fore.YELLOW}{self.name}: Checkout tags")
        self.checkout_tags()
        print(f"[RELEASE]{Fore.YELLOW}{self.name}: Set last release version")
        # self.update_git_tag_versions()
        self.fetch_github_versions()

    def switch_to_develop(self):
        print(f"[DEVELOP]{Fore.YELLOW}{self.name}: Switch branch to 'develop'")
        self.switch_branch("develop")
        print(f"[DEVELOP]{Fore.YELLOW}{self.name}: Checkout 'develop' branch")
        self.checkout_branch()
        print(f"[DEVELOP]{Fore.YELLOW}{self.name}: Checkout tags")
        self.checkout_tags()
        # print(f"[DEVELOP]{Fore.YELLOW}{self.name}: Set last release version")
        # self.update_git_tag_versions()
        # self.fetch_github_versions()

        # Check all dependency is in master branch
        for _dep in self.dependencies:
            if _dep.branch != "master":
                print(f"{Fore.RED} Error in {self.name} - Dependency: {_dep.name} not in master branch, "
                      f"instead of: {_dep.branch}")
                raise SystemExit(1)


def process_module(par, _modules, _module_by_name):
    if type(par) is dict:
        _new_module = Module(par)
        _modules.append(_new_module)
        _module_by_name[_new_module.name] = _new_module
    elif type(par) is list:
        for _item in par:
            process_module(_item, _modules, _module_by_name)


def load_modules(_filename="project-meta.yml", _str=""):
    if _filename:
        with open(_filename, 'r') as stream:
            try:
                _results = yaml.load(stream, Loader=yaml.FullLoader)
                return _results
            except yaml.YAMLError as exc:
                print(exc)
                raise exc
    elif _str:
        try:
            _results = yaml.load(_str, Loader=yaml.FullLoader)
            return _results
        except yaml.YAMLError as exc:
            print(exc)
            raise exc
    else:
        raise SystemExit(f"filename or str have to be defined")


def calculate_graph(_modules):
    _g = nx.DiGraph()
    for _module in _modules:
        _g.add_node(_module)

    for _module in _modules:
        for _dependency in _module.dependencies:
            if _dependency in _modules:
                _g.add_edge(_dependency, _module)
    return _g


def calculate_reduced_graph(_modules):
    _gt = transitive_reduction(calculate_graph(_modules))
    return _gt


def calculate_ranks(_modules):
    _g = calculate_graph(_modules)
    _groups = list(topological_sort_grouped(_g))
    _rank = 0
    for _group in _groups:
        _rank += 1
        for _module in _group:
            _module.rank = _rank

def slice_modules(_modules, _module_name, _begin):
    _sliced_modules = []
    _g = calculate_graph(_modules)
    _groups = list(topological_sort_grouped(_g))
    _found = False
    for _group in _groups:
        for _module in _group:
            if _module.name == _module_name:
                _found = True
            if _begin and not _found:
                _sliced_modules.append(_module)
            if not _begin and _found:
                _sliced_modules.append(_module)
    return _sliced_modules



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


def check_module_depenencies(_modules, _module_by_name, _fix_dependencies=False):
    _errors = []
    _pending_changes = False
    for _module in _modules:
        if not _module.virtual:
            for _module_to_check in _modules:
                version_in_pom = current_pom_version(_module, _module_to_check)
                if version_in_pom:
                    if _module_to_check not in _module.dependencies:
                        print(f"{Fore.GREEN}{_module.name}{Fore.YELLOW} - doesn't contain "
                              f"{Fore.GREEN}{_module_to_check.name}{Fore.YELLOW} in dependencies, but "
                              f"{Fore.GREEN}{_module.path}/pom.xml{Fore.YELLOW} have "
                              f"{Fore.GREEN}{_module_to_check.property}")
                        if _fix_dependencies:
                            _module.dependencies.append(_module_to_check)
                            _pending_changes = True
                else:
                    if _module_to_check in _module.dependencies:
                        _errors.append(f"{Fore.RED}{_module.name} - Property definition "
                                       f"{Fore.GREEN}{_module_to_check.property}{Fore.RED} is missing in "
                                       f"{Fore.GREEN}{_module.path}/pom.xml")

    if len(_errors) > 0:
        for _error in _errors:
            print(f"{_error}\n")
        raise SystemExit(f"\n{Fore.RED}Errors found in module dependencies.")
    return _pending_changes


def print_dependency_graph(_modules):
    _g = calculate_reduced_graph(_modules)
    for node in _g.nodes():
        _g.nodes[node]['shape'] = 'box'
        _g.nodes[node]['label'] = f"{node.name} ({node.rank})"
    write_dot(_g, "dependency.dot")
    to_pydot(_g).write_svg("dependency.svg")

def print_dependency_graph_ascii(_modules):
    _available_modules = set(filter(lambda _m: not _m.ignored and not _m.virtual, _modules))
    _g = calculate_graph(_available_modules)
    _groups = list(topological_sort_grouped(_g))
    for idx in range(len(_groups)):
        print(str(idx + 1) + " - " + str(_groups[idx]))

def get_request_handler(_modules, _process_info):
    class MyHandler(http.server.BaseHTTPRequestHandler):

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()

        def do_GET(self):
            body = ""
            etag = ""
            _g = nx.DiGraph()
            for _module in _process_info.keys():
                _g.add_node(_module)

            for _module in _modules:
                for dependency in _module.dependencies:
                    if dependency in _modules and dependency in _process_info.keys():
                        _g.add_edge(_module, dependency)

            _g = transitive_reduction(_g)
            for node in _g.nodes():
                _g.nodes[node]['shape'] = 'box'
                _g.nodes[node]['label'] = f"{node.name} ({node.rank})"
                _g.nodes[node]['fillcolor'] = 'azure3'
                _g.nodes[node]['style'] = 'filled'
                if _process_info.get(node, {"status": "UNKNOWN"}).get("status") == "UNKNOWN":
                    _g.nodes[node]['fillcolor'] = 'wheat'
                if _process_info.get(node, {"status": "WAITING"}).get("status") == "WAITING":
                    _g.nodes[node]['fillcolor'] = 'skyblue'
                if _process_info.get(node, {"status": ""}).get("status") == "RUNNING":
                    _g.nodes[node]['fillcolor'] = "yellow"
                if _process_info.get(node, {"status": ""}).get("status") == "OK":
                    _g.nodes[node]['fillcolor'] = "green"
                if _process_info.get(node, {"status": ""}).get("status") == "IDLE":
                    _g.nodes[node]['fillcolor'] = "lightslategrey"
                if _process_info.get(node, {"status": ""}).get("status") == "ERROR":
                    _g.nodes[node]['fillcolor'] = "red"

            dot_body = "<div id=\"dot\">" + to_pydot(_g).to_string() + "</div>"

            if self.path.endswith('/svg'):
                svg = to_pydot(_g).create_svg().decode("utf-8")
                message_bytes = svg.encode('ascii')
                base64_bytes = base64.b64encode(message_bytes)
                body = "<embed src=\"data:image/svg+xml;base64," + base64_bytes.decode('ascii') + "\"/>"

            elif self.path.endswith('/dot'):
                body = dot_body
                md5 = hashlib.md5(body.encode('utf-8')).hexdigest()
                req_md5 = self.headers['If-None-Match']
                req_modified_since = self.headers["If-Modified-Since"]
                etag = md5
                if ("\"" + md5 + "\"") == req_md5 or ("\"" + md5 + "\"") == req_modified_since:
                    self.send_response(304)
                    self.send_header("ETag", "\"" + etag + "\"")
                    # self.send_header("Last-Modified", format_datetime(datetime.now(timezone.utc), usegmt=True))
                    self.send_header("Last-Modified", "\"" + etag + "\"")
                    self.end_headers()
                    return
            else:
                body = ('''
                        <!doctype html>
                        <html lang="en">
                        <head>
                            <meta charset="utf-8"/>
                            <meta name="viewport" content="width=device-width" />
                            <title>project.py</title>
                            <script src="https://unpkg.com/htmx.org@1.9.5"></script>
                            <script src="https://d3js.org/d3.v5.min.js"></script>
                            <script src="https://unpkg.com/@hpcc-js/wasm@0.3.11/dist/index.min.js"></script>
                            <script src="https://unpkg.com/d3-graphviz@3.0.5/build/d3-graphviz.js"></script>
                       </head>
                       <body style="margin: 0;padding: 0;">
                            <div id="graph" style="text-align: center;"></div>
                            <div style="width: 0px; height:0px; visibility: hidden" hx-get="/dot" hx-swap="innerHTML" hx-trigger="every 5s">''' + dot_body + '''</div>
                            <script>
                                var margin = 20; // to avoid scrollbars
                                var width = window.innerWidth - margin;
                                var height = window.innerHeight - margin - 40;
                                var graphviz = d3.select("#graph").graphviz()
                                    .transition(function () {
                                        return d3.transition("main")
                                            .ease(d3.easeLinear)
                                            .delay(0)
                                            .duration(750);
                                    })
                                    .zoom(true);

                                function render() {
                                    graphviz
                                        .width(width)
                                        .height(height)
                                        .renderDot(document.getElementById("dot").textContent);
                                }
                                document.addEventListener("resize", function(evt) {
                                    var width = window.innerWidth - margin;
                                    var height = window.innerHeight - margin - 40;
                                    render();
                                });
                                document.addEventListener('htmx:afterRequest', function(evt) {
                                    render();
                                });
                                document.addEventListener('DOMContentLoaded', function(evt) {
                                    render();
                                });
                            </script>
                       </body>
                       </html>''').strip()

            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", "\"" + etag + "\"")
            self.send_header("Last-Modified", "\"" + etag + "\"")
            self.end_headers()
            self.wfile.write(bytes(body, "utf-8"))

        def log_message(self, format, *args):
            return

    return MyHandler


def start_process_info_server(_modules, _process_info):
    _port = 8000
    while is_port_in_use(_port):
        _port += 1

    print(f"{Fore.YELLOW}Serving at port:{Fore.GREEN}{_port}")
    server_address = ('localhost', _port)
    httpd: StoppableHTTPServer = StoppableHTTPServer(server_address, get_request_handler(_modules, _process_info))
#    threading.Thread(target=lambda: webbrowser.open_new_tab(f"http://127.0.0.1:{_port}")).start()
    return httpd


def calculate_modules_to_update(_modules, _module_by_name, _active_processes=None, _release_build=False,
                                _already_processed_modules=None, _check_pom_change=True):
    if _active_processes is None:
        _active_processes = set()
    if _already_processed_modules is None:
        _already_processed_modules = set()

    _updated_modules = set()
    for _module in _modules:
        if not _module.ignored and not _module.virtual and _module not in _already_processed_modules:
            if not _check_pom_change:
                _updated_modules.add(_module)
            else:
                # print("Checking dependency update in POM: " + _module.name)
                if _release_build:
                    _master_deps_only = len(list(filter(lambda _dep_module: _dep_module.branch != 'master',
                                                        _module.dependencies))) == 0
                    if (_master_deps_only and _module.branch == 'develop') or \
                            _module.update_dependency_versions_in_pom(False):
                        _updated_modules.add(_module)

                if not _release_build and _module.update_dependency_versions_in_pom(False):
                    _updated_modules.add(_module)

    # Removing modules which have dependency on current modules
    _removable_modules = set()
    for _module in _updated_modules.union(_active_processes):
        _removable_modules = _removable_modules.union(set(
            descendants(calculate_graph(_modules), _module)))

    _updated_modules = _updated_modules.difference(_removable_modules)
    return _updated_modules


def update_modules_versions_in_pom_and_push(_modules,
                                            _module_by_name,
                                            _active_processes=None,
                                            _already_processed_modules=None):
    _updated_modules = calculate_modules_to_update(_modules, _module_by_name,
                                                   _active_processes=_active_processes,
                                                   _release_build=False,
                                                   _already_processed_modules=_already_processed_modules)

    print(f"\n{Fore.RED}Versions updated for modules :\n" + (
        f"\n".join(map(lambda _m: f"{Fore.GREEN}-{_m.name} ({_m.rank}){Style.RESET_ALL}",
                       _updated_modules))) + f"{Style.RESET_ALL}\n")

    for _module in _updated_modules:
        if args.update_pom or args.integration_build or args.release_build:
            print(f"{Fore.YELLOW}Writing changes to {Fore.GREEN}{_module.name}")
            _module.update_dependency_versions_in_pom(True)

        if (args.update_pom or args.integration_build or args.release_build) and not args.run_postchangescripts:
            print("Running post change scripts")
            if not _module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {_module.name}.")
                if not args.dirty:
                    save_modules(modules, _module_by_name)
                raise SystemExit(1)
        if args.push_updates or args.integration_build or args.release_build:
            print("Pushing updates to " + _module.name)
            _module.commit_and_push_changes()
    return _updated_modules


# =============================== Initial version fetching

def fetch_versions(_modules, _fetch_ignored=False):
    for _module in _modules:
        if not _module.ignored or _fetch_ignored:
            print(
                f"{Fore.YELLOW}Checking latest release for {Fore.GREEN}{_module.name}{Fore.YELLOW} in branch: "
                f"{Fore.GREEN}{_module.branch}")
            if _fetch_ignored:
                # TODO: Virtual modules does not have git representation, so fetching is not possible
                _module.fetch_github_versions()
            else:
                # _module.update_git_tag_versions()
                _module.fetch_github_versions()


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
        print(f"\n{Fore.RED}Error execution before local build script for {_module.name}. ")
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

    _log = tempfile.NamedTemporaryFile()
    cmd = "mvn clean install " + (" ".join(list(map(lambda x: f"-D{x}", arguments))))
    print(f"{Fore.YELLOW}Calling maven: \n\t{Fore.CYAN}{cmd}{Style.RESET_ALL}\n")
    print(
        f"{Fore.GREEN}================================================================================"
        f"{Style.RESET_ALL}")

    with open(_log.name, 'w') as f:
        _ret = call(cmd, shell=True, cwd=_currentDir, stdout=f, stderr=f)

    if not _module.call_afterlocalbuildscripts():
        print(f"\n{Fore.RED}Error execution after local build script for {_module.name}. ")
        return False

    if not args.keep_changes:
        # Restore versions properties to original values in pom.xml
        if len(_original_versions) > 0:
            for _versionName in _original_versions.keys():
                _ref_prop_element = _properties.find(pom_namespace + _versionName)
                _ref_prop_element.text = _original_versions[_versionName]
            _pom.write(_module.path + "/pom.xml", encoding="UTF-8")

    if _ret != 0:
        with open(_log.name, 'r') as f:
            shutil.copyfileobj(f, sys.stdout)
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

def modules_with_branch(_branch_name, _modules):
    _branch_modules = set()
    for _module in _modules:
        if _module.branch == _branch_name:
            if _module.virtual:
                raise SystemExit(f"\n{Fore.RED}Virtual module cannot be used in snapshot mode: {_module_name}.")
            _branch_modules.add(_module)
    return _branch_modules


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

    if len(args.modules_with_branch) > 0:
        _retry_command = _retry_command + "-bm " + args.modules_with_branch[0] + " "

    return _retry_command


def calculate_processable_modules(_modules, _process_info, _module_by_name):
    _available_modules = set(filter(lambda _m: not _m.ignored and not _m.virtual, _modules))
    if args.modules_with_branch:
        print(f"Calculate modules with branch: {args.modules_with_branch[0]}" )
        return modules_with_branch(args.modules_with_branch[0], _available_modules)

    _ignored_modules = ignored_modules(_module_by_name).union(
        set(filter(lambda _m: _m.virtual or _m.ignored, _modules)))
    _start_modules = start_modules(_module_by_name)
    _modules_to_build = set(_available_modules)
    _terminate_modules = terminate_modules(_module_by_name)

    # if len(_start_modules) > 0:
    #     print(f"{Fore.YELLOW}Start modules:{Style.RESET_ALL} " + (
    #         ", ".join(map(lambda _m: _m.name, list(_start_modules)))))

    if len(_start_modules) == 0:
        _g = calculate_graph(_available_modules)
        _groups = list(topological_sort_grouped(_g))
        if len(_groups) > 0:
            _start_modules = _groups[0]

    if len(_terminate_modules) > 0:
        for _module_terminate in _terminate_modules:
            # removing terminate modules descendants
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
        _start_modules_descendants = set()
        _terminate_ancestors = set()
        _terminate_descendants = set()

        for _module in _start_modules:
            _start_modules_descendants = _start_modules_descendants.union(set(descendants(calculate_graph(_available_modules),
                                                                                    _module)))
            # print(f"{Fore.BLUE}{_module.name} {Style.RESET_ALL} descendants (all): " + (
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
                # the required modules is the intersection of terminate modules' descendants and module's ascendants

        _calculated_modules = _start_modules_descendants
        if len(_terminate_descendants) > 0 or len(_terminate_ancestors) > 0:
            _calculated_modules = _start_modules_descendants.union(_start_modules) \
                .intersection(_terminate_ancestors.union(_terminate_modules))

            # print(f"{Fore.BLUE}{_module.name} {Style.RESET_ALL} descendants (reduced with terminated): " + (
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


def run_mvn_process(*args, **kwargs):
    _module = kwargs['module']
    _version_args = kwargs['version_args']
    _status = build_module(_module, _version_args)

    if not _status:
        raise Exception(f"\n{Fore.RED}Error when building {_module.name}.")


def build_snapshot(_modules, _process_info, _module_by_name):
    _ignored_modules = ignored_modules(_module_by_name)

    _version_args = []
    for _module in _modules:
        _version_args.append(_module.property + "=" + current_snapshot_version(_module))

    print(f"{Fore.YELLOW}Versions override:{Style.RESET_ALL}\n\t" + f"\n\t".join(
        map(lambda _v: f"{Fore.WHITE}{_v.split('=')[0]} {Fore.YELLOW}={Fore.GREEN} {_v.split('=')[1]}{Style.RESET_ALL}",
            _version_args)))

    for _module in set(_modules):
        _process_info[_module] = {"status": "WAITING"}

    if args.continue_module:
         _start_module_name = args.continue_module[0]
         for _module in slice_modules(_modules, _start_module_name, True):
             _process_info[_module] = {"status": "IDLE"}
         _modules = slice_modules(_modules, _start_module_name, False)

    _retry_command = './project.py -bs ' + calc_retry_command_for_build(_modules, _process_info, _module_by_name)
    _already_processed_modules = set()
    _current_threads = {}
    _modules_to_process = _modules.copy()
    _errors = []
    while len(set(_modules).difference(_already_processed_modules)) > 0 or len(_current_threads) > 0:
        if len(_current_threads) < args.parallel and len(_errors) == 0:

            # Calculate independent modules to start processing
            _removable_modules = set()

            # Recalculate modules to process
            for _module in set(_modules).difference(_already_processed_modules):
                _removable_modules = _removable_modules.union(set(
                    descendants(calculate_graph(_modules), _module)))

            _modules_to_process = list(set(_modules).difference()
                                   .difference(_current_threads.keys())
                                   .difference(_already_processed_modules)
                                   .difference(_removable_modules))

            if len(_modules_to_process) > 0:
                    _module = _modules_to_process.pop(0)
                    _thread_obj = PropagatingThread(target=run_mvn_process, args=(),
                                                   kwargs={ 'version_args': _version_args, 'module': _module })
                    _thread_obj.start()
                    _current_threads[_module] = _thread_obj
                    _process_info[_module] = {"status": "RUNNING"}

        _threads_to_remove = set([])
        for _module, _thread_obj in _current_threads.items():
            try:
                _thread_obj.join(0.5)
            except Exception as e:
                traceback.print_exception(*sys.exc_info())
                _threads_to_remove.add(_module)
                _process_info[_module] = {"status": "ERROR"}
                _errors.append(_module)
                print(f"\n{Fore.RED}Error when building: " + ", ".join(map(lambda _m: _m.name, _errors)) + ". "                                                                                                              f"\nTo retry, run {Fore.YELLOW}{_retry_command} -c {_module.name}{Style.RESET_ALL}")
                print(f"\nWaiting to finish other processing modules. Hit CTRL-C to force quit\n")

            if not _thread_obj.is_alive():
                if _module not in _errors:
                    _process_info[_module] = {"status": "OK"}
                _threads_to_remove.add(_module)

        for _module in _threads_to_remove:
            _already_processed_modules.add(_module)
            del _current_threads[_module]

        if len(_current_threads) == 0 and len(_errors) > 0:
            time.sleep(10)
            raise SystemExit(f"\n{Fore.RED}Error when building: " + ", ".join(map(lambda _m: _m.name, _errors)) + ". "
                     f"\nTo retry, run {Fore.YELLOW}{_retry_command} -c {_module.name}{Style.RESET_ALL}")



def check_release_modules_for_error(_modules):
    _error = False
    for _module in _modules:
        _wrong_dependencies = list(filter(lambda _dep_module: _dep_module.branch != "master", _module.dependencies))
        if len(_wrong_dependencies) > 0:
            for _wrong_dependency in _wrong_dependencies:
                print(f"\n{Fore.RED}Dependency {_wrong_dependency.name} is not in 'master' for {_module.name}\n")
                _error = True

        if "perform-release-on-" + _module.version in _module.get_remote_tags().keys():
            print(f"\n{Fore.RED}Tag 'perform-release-on-{_module.version}' already set for  {_module.name}\n")
            _error = True

    return _error

def build_continuous(_modules, _process_info, _module_by_name, _release_build=False):
    print(f"\n{Fore.YELLOW}Continuous build for modules :\n" + (
        f"\n".join(map(lambda _m: f"- {Fore.GREEN}{_m.name} ({_m.rank}){Style.RESET_ALL}",
                       _modules))) + f"{Style.RESET_ALL}\n")

#    for _module in calculate_modules_to_update(_modules, _module_by_name, _release_build=_release_build):
    _error = False
    for _module in _modules:
        if (not args.integration_build) and (_module.branch not in ["master", "develop"]):
            print(f"\n{Fore.RED}To release '{_module.name}' branch have to be 'develop' or 'master'. ")
            _error = True

        if not _release_build and _module.update_dependency_versions_in_pom(False) and _module.branch == "master":
            print(f"\n{Fore.RED}There is some update in dependency versions while '{_module.name}' branch is 'master'. "
                  f"Please switch to develop or set it ignored.\n")
            _error = True

    if _error:
        raise SystemExit(1)

    if _release_build:
        # Collect changed modules and modules which in 'develop' branch but all versions are master
        _candidate_modules = calculate_modules_to_update(_modules, _module_by_name, _release_build=True)

        print(f"\n{Fore.YELLOW}Plan to release modules :\n" +
              (f"\n".join(map(lambda _m: f"- {Fore.GREEN}{_m.name} ({_m.rank}){Style.RESET_ALL}", _modules)))
              + f"{Style.RESET_ALL}\n")

        print(f"\n{Fore.YELLOW}Starting with modules :\n" +
              (f"\n".join(map(lambda _m: f"- {Fore.GREEN}{_m.name} ({_m.rank}){Style.RESET_ALL}", _candidate_modules)))
              + f"{Style.RESET_ALL}\n")

        if check_release_modules_for_error(_candidate_modules):
            raise SystemExit(1)

        for _module in _candidate_modules:
            if _module.branch == 'master':
                _module.switch_to_develop()

        _modules_to_process = update_modules_versions_in_pom_and_push(_modules, _module_by_name)

        # Collect modules which is in 'develop' branch and only master dependencies are defined, because commit
        # will not be performed, but have to tag
        _modules_to_perform_release = _candidate_modules.difference(_modules_to_process)

        if check_release_modules_for_error(_modules_to_perform_release):
            if not args.dirty:
                save_modules(modules, _module_by_name)
            raise SystemExit(1)

        # Check tag already exists
        for _module in _modules_to_perform_release:
            _module.perform_release()
        if not args.dirty:
            save_modules(modules, _module_by_name)

        _active_processes = _modules_to_process.union(_modules_to_perform_release)
    else:
        _active_processes = update_modules_versions_in_pom_and_push(_modules, _module_by_name)

    for _module in _modules:
        _process_info[_module] = {"status": "WAITING"}

    _already_processed_modules = set()
    while len(_active_processes) > 0:
        # wait_for_modules_to_release(process_info, current_updated_dependency_in_modules)
        time.sleep(15)
        _removable_modules = set()
        for _module in _active_processes:

            _process_info[_module] = {"status": "RUNNING"}
            print(
                f"{Fore.YELLOW}Checking latest release for {Fore.GREEN}{_module.name}{Fore.YELLOW} in branch: "
                f"{Fore.GREEN}{_module.branch}")
            if _module.fetch_github_versions():

                print("  NEW Version, it removed from wait list")
                _process_info[_module] = {"status": "OK"}
                if _release_build and _module.branch == "develop":
                    if check_release_modules_for_error([_module]):
                        if not args.dirty:
                            save_modules(modules, _module_by_name)
                        raise SystemExit(1)
                    _module.perform_release()
                    if not args.dirty:
                        save_modules(modules, _module_by_name)
                elif _release_build and _module.branch == "master":
                    print(f"[RELEASE]{Fore.YELLOW}{_module.name}: Checkout 'master' branch")
                    _module.checkout_branch()
                    # _module.ignored = True
                    _removable_modules.add(_module)
                    if not args.dirty:
                        save_modules(modules, _module_by_name)
                    print(f"[RELEASE]{Fore.YELLOW}{_module.name}: FINISHED")
                else:
                    _removable_modules.add(_module)

        if len(_removable_modules) > 0:
            _active_processes = _active_processes.difference(_removable_modules)
            _already_processed_modules = _already_processed_modules.union(_removable_modules)

            _new_modules_candidates = calculate_modules_to_update(_modules, _module_by_name,
                                                                  _active_processes=_active_processes,
                                                                  _already_processed_modules=_already_processed_modules)

            if _release_build:
                for _module in _new_modules_candidates:
                    if _module.branch == 'master':
                        _module.switch_to_develop()

            _new_modules_to_process = update_modules_versions_in_pom_and_push(_modules, _module_by_name,
                                                                              _active_processes=_active_processes,
                                                                              _already_processed_modules
                                                                              =_already_processed_modules)

            for _new_module in _new_modules_to_process.union(_active_processes):
                _removable_modules = _removable_modules.union(set(
                    descendants(calculate_graph(_modules), _new_module)))

            _new_modules_to_process = _new_modules_to_process.union(_active_processes)
            _active_processes = _new_modules_to_process.difference(_removable_modules)

            print(f"\n{Fore.YELLOW}Waiting for modules :\n" + (
                f"\n".join(map(lambda _m: f"{Fore.GREEN}{_m.name} ({_m.rank}){Style.RESET_ALL}",
                               _active_processes))) + f"{Style.RESET_ALL}\n")

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
    process_info_server_pid.daemon = True
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


def checkout_with_progressbar(*args, **kwargs):
    _module = kwargs['module']
    _pbar = kwargs['progress']

    _submodule_repo = _module.repo()
    #print(f"{Fore.YELLOW}Checkout submodule {Fore.GREEN}{module.branch} {Fore.YELLOW} in "
    #      f"{Fore.GREEN}{submodule_repo.git_dir}")
    if _module.check_dirty():
        print(f"{Fore.YELLOW} {_module.name} Submodule is in dirty state, stashing")
        _module.repo().git.stash(["-u", "-m \"[AUTO STASH]\""])

    if not os.path.exists(_module.path):
        # print(f"{Fore.YELLOW}Update submodule {Fore.GREEN}{_module.name}{Style.RESET_ALL}")
        repo.submodule(_module.path).update(init=True, to_latest_revision=True, force=True)

    # print(f"{Fore.YELLOW}Set branch {Fore.GREEN}{_module.branch}{Fore.YELLOW} for {Fore.GREEN}{_module.name}
    # {Style.RESET_ALL}")
    _module.checkout_branch()
    _module.checkout_tags()
    _pbar.update(1)
    return


# ============================== Mandatory module init
print(f"{Fore.YELLOW}Load modules")
for item in load_modules():
    process_module(item, modules, module_by_name)

for module in modules:
    # print("Resolve dependencies: " + module.name)
    module.resolve_dependencies(module_by_name)

calculate_ranks(modules)

processable_modules = calculate_processable_modules(modules, process_info, module_by_name)

print_dependency_graph_ascii(processable_modules)

# =============================== Fetch / Checkout / Reset Git
if args.git_checkout or args.release_build:
    currentDir = os.getcwd()
    repo = git.Repo(currentDir)
    _processable_modules = processable_modules.copy()
    _current_threads = {}
    print("\n")
    with tqdm(total=len(_processable_modules), colour="green", bar_format='{desc:<60}{percentage:3.0f}%|{bar}{r_bar}') as _pbar:
        while len(_processable_modules) > 0 or len(_current_threads) > 0:
            if len(_current_threads) < args.parallel and len(_processable_modules) > 0:
                _module = _processable_modules.pop(0)
                if not _module.virtual:
                    _thread_obj = PropagatingThread(target=checkout_with_progressbar, args=(),
                                                    kwargs={ 'progress': _pbar, 'module': _module })
                    _thread_obj.start()
                    _current_threads[_module] = _thread_obj
                    _pbar.set_description(f", ".join(map(lambda _m: f"{Fore.YELLOW}{_m.name}{Fore.GREEN}({_m.branch})",
                                                         _current_threads.keys())), refresh=True)
            _threads_to_remove = []
            for _module, _thread_obj in _current_threads.items():
                try:
                    _thread_obj.join(0.1)
                except Exception as e:
                    traceback.print_exception(*sys.exc_info())
                    _threads_to_remove.append(_module)
                    raise SystemExit(f"\n{Fore.RED}Error when fetching {_module.name}. ")

                if not _thread_obj.is_alive():
                    _threads_to_remove.append(_module)

            for _module in _threads_to_remove:
                del _current_threads[_module];

            _pbar.set_description(f", ".join(map(lambda _m: f"{Fore.YELLOW}{_m.name}{Fore.GREEN}({_m.branch})",
                                                 _current_threads.keys())), refresh=True)

pending_changes = check_module_depenencies(modules, module_by_name, _fix_dependencies=args.fix_dependencies)
if not args.dirty and pending_changes:
    save_modules(modules, module_by_name)

if args.fetch_versions or args.integration_build or args.release_build:
    fetch_versions(processable_modules)

if args.fetch_versions_all:
    fetch_versions(modules, _fetch_ignored=True)

if args.new_feature:
    for module in processable_modules:
        if not module.virtual and not module.ignored:
            new_feature_parameters = args.new_feature
            if len(new_feature_parameters) > 2:
                print(f"{Fore.RED} WARNING! "
                      f"{Fore.YELLOW}new feature parameters are ignored from the 3rd one and onwards.")

            module.create_branch(new_feature_parameters[0])

            if len(new_feature_parameters) > 1 and new_feature_parameters[1]:
                module.create_pull_request(args.new_feature[1])
            else:
                module.create_pull_request(new_feature_parameters[0])

if args.update_branch:
    for module in processable_modules:
        if not module.virtual and not module.ignored:
            module.update_branch_from_git()

if args.create_branch and not args.new_feature:
    for module in processable_modules:
        if not module.virtual and not module.ignored:
            module.create_branch(args.create_branch[0])

if args.switch_branch and not args.new_feature:
    for module in processable_modules:
        if not module.virtual and not module.ignored:
            module.switch_branch(args.switch_branch[0])

if args.create_pr and not args.new_feature:
    for module in processable_modules:
        if not module.virtual and not module.ignored:
            module.create_pull_request(args.create_pr[0])

if args.update_module_pom:
    for module in processable_modules:
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
    server_start(processable_modules, process_info)
    for module in processable_modules:
        process_info[module] = {"status": "IDLE"}
    build_snapshot(processable_modules, process_info, module_by_name)

# =============================== Generating Graphviz
if args.graphviz:
    print_dependency_graph(modules)

# =============================== Force postchange run
if args.run_postchangescripts:
    for module in processable_modules:
        if not module.ignored and not module.virtual:
            if not module.call_postchangescripts():
                print(f"\n{Fore.RED}Error when calling post change script on {module.name}.")
                if not args.dirty:
                    save_modules(modules, module_by_name)
                raise SystemExit(1)
            if args.push_updates or args.integration_build or args.release_build:
                module.commit_and_push_changes()

# ================================ Checking for updates
if args.continuous_update or args.integration_build or args.release_build:
    server_start(processable_modules, process_info)
    for module in processable_modules:
        process_info[module] = {"status": "IDLE"}
    build_continuous(processable_modules, process_info, module_by_name, _release_build=args.release_build)

if args.relnotes:
    currentDir = os.getcwd()
    _rootRepo = git.Repo(os.getcwd())
    hashes = args.relnotes[0].split("..")
    if len(hashes) == 2:
        current_modules_loaded = load_modules(_filename='', _str=_rootRepo.git.show(hashes[1] + ":project-meta.yml"))
    else:
        current_modules_loaded = load_modules()

    current_modules_by_name = {}
    current_modules = []

    for item in current_modules_loaded:
        process_module(item, current_modules, current_modules_by_name)

    previous_modules_loaded = load_modules(_filename='', _str=_rootRepo.git.show(hashes[0] + ":project-meta.yml"))

    previous_modules_by_name = {}
    previous_modules = []
    for item in previous_modules_loaded:
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
        if module.name in module_versions and not module.ignored:
            if module.version != module_versions[module.name][1]:

                print(f"Getting logs for {module.name}")

                from_tag = module_versions[module.name][1]
                to_tag = module_versions[module.name][0]

                tags = module.get_remote_tags()

                from_sha = ''
                to_sha = ''
                for tag in tags:
                    short_sha = module.repo().git.rev_parse(tags[tag], short=True)
                    # print(f"{tagref} {short_sha}")
                    if f"{tag}" == from_tag or f"{tag}" == f"v{from_tag}":
                        from_sha = short_sha
                    if f"{tag}" == to_tag or f"{tag}" == f"v{to_tag}":
                        to_sha = short_sha

                # Local tag checkout
                # module.checkout_tags()
                # for tagref in module.repo().tags:
                #     short_sha = module.repo().git.rev_parse(tagref.commit, short=True)
                #     if f"{tagref}" == from_tag or f"{tagref}" == f"v{from_tag}":
                #         from_sha = short_sha
                #     if f"{tagref}" == to_tag or f"{tagref}" == f"v{to_tag}":
                #         to_sha = short_sha

                if not from_sha:
                    print(f"Version tag: {from_tag} not found for {module.name}")
                if not to_sha:
                    print(f"Version tag: {to_tag} not found for {module.name}")

                if from_sha and to_sha:
                    for line in module.repo().git.log(
                            '{}..{} --pretty=oneline'.format(from_sha, to_sha).split()).splitlines():
                        m = re.search('JNG-\\d+', line)
                        if m:
                            print(f"Commit: {line}")
                            issues.add(m.group())

    for module in new_modules:
        for line in module.repo().git.log('--pretty=oneline'.split()).splitlines():
            m = re.search('JNG-\\d+', line)
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
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-macosx.cocoa.x86_64.tar.gz[MacOS Intel] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-macosx.cocoa.aarch64.tar.gz[MacOS Apple Silicon] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-linux.gtk.x86_64.tar.gz[Linux x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-linux.gtk.aarch64.tar.gz[Linux Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-linux.gtk.x86_64_all.deb[Linux Debian/Ubuntu x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-linux.gtk.aarch64_all.deb[Linux Debian/Ubuntu Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-win32.win32.x86_64.zip[Windows ZIP] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-designer/" \
              f"{module_by_name['judo-epp-designer'].version}/judo-designer_" \
              f"{module_by_name['judo-epp-designer'].version}-win32.win32.x86_64.exe[Windows Installer] |\n"
    output += "\n"
    output += "Download Architect\n"
    output += "------------------\n"
    output += "\n"
    output += f"Version: {module_by_name['judo-epp-architect'].version}\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-macosx.cocoa.x86_64.tar.gz[MacOS Intel] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-macosx.cocoa.aarch64.tar.gz[MacOS Apple Silicon] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-linux.gtk.x86_64.tar.gz[Linux x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-linux.gtk.aarch64.tar.gz[Linux Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-linux.gtk.x86_64_all.deb[Linux Debian/Ubuntu x86_64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-linux.gtk.aarch64_all.deb[Linux Debian/Ubuntu Arm64] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-win32.win32.x86_64.zip[Windows ZIP] |\n"
    output += f"https://nexus.judo.technology/repository/p2/judo-epp-architect/" \
              f"{module_by_name['judo-epp-architect'].version}/judo-architect_" \
              f"{module_by_name['judo-epp-architect'].version}-win32.win32.x86_64.exe[Windows Installer] |\n"
    output += "\n"
    output += "JUDO Eclipse development plugin site\n"
    output += "------------------------------------\n"
    output += f"Version: {module_by_name['judo-eclipse-development'].version}\n"
    output += "\n"
    output += "The JUDO Architect does not contain any JUDO meta models or JQL / JCL plugins. This update site " \
              "contains all required artifacts which are used to build Eclipse Designer.\n"
    output += f"To install plugins open `Install new Software` window and in update site " \
              f"add `https://nexus.judo.technology/repository/p2/judo-eclipse-development/" \
              f"{module_by_name['judo-eclipse-development'].version}`/  or use the "\
              f"`https://nexus.judo.technology/repository/p2/judo-eclipse-development/develop " \
              f"for daily builder current version"
    output += "To update a previously installed version, set the update site URL to the desired one and update the " \
              "plugin.\n"
    output += "\n"
    output += "JUDO Modules\n"
    output += "------------\n"
    output += "\n"
    output += "|=======================\n"
    output += "| Name | GitHUB | Version\n"
    for module in modules:
        output += f"| {module.name} | https://github.com/{module.github}[{module.github}] | https://github.com/" \
                  f"{module.github}/releases/tag/v{module.version}[{module.version}^]\n"
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
            output += f"| https://blackbelt.atlassian.net/browse/{issueNumber}[{issueNumber}^] | {issuetype} | " \
                      f"{summary} | {status} | {note}\n"
        except BaseException as err:
            print(f"An exception occurred on fetching {issueNumber} - {err=}, {type(err)=}")
    output += "|=======================\n"

    relnotes_file = open("relnotes_" + now.strftime("%Y_%m_%d") + ".adoc", "w")
    relnotes_file.write(output)
    relnotes_file.close()
