#!/usr/bin/env python3

# Copyright 2019 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Migrates information from Testgrid's Config.yaml to a subdirectory of Prow Jobs

Moves Dashboard Tabs and redundant Test Groups
Skips any Configuration that contains unusual keys, even if they're incorrect keys.
"""

import re
import argparse
from os import walk
import ruamel.yaml

# Prow files that will be ignored
PROW_BLACKLIST = [
    # Ruamel won't be able to successfully dump k8s-triage-robot-periodics
    # See https://bitbucket.org/ruamel/yaml/issues/258/applying-json-patch-breaks-comment
    "k8s-triage-robot-trusted.yaml",
    # Generated security jobs are generated with the same name as kubernetes/kubernetes
    # presubmits, but we never want to migrate to the generated ones.
    "generated-security-jobs.yaml",
    # generated.yaml is generated by generate_tests.py, and will be overwritten.
    "generated.yaml",
]
MAX_WIDTH = 2000000000


def main(testgrid_config, prow_dir):
    with open(testgrid_config, "r") as config_fp:
        config = ruamel.yaml.load(config_fp,
                                  Loader=ruamel.yaml.RoundTripLoader,
                                  preserve_quotes=True)

    for dashboard in config["dashboards"]:
        if "dashboard_tab" in dashboard:
            for dash_tab in dashboard["dashboard_tab"][:]:
                if assert_tab_keys(dash_tab):
                    move_tab(dashboard, dash_tab, prow_dir)

    if "test_groups" in config:
        for test_group in config["test_groups"][:]:
            if assert_group_keys(test_group):
                move_group(config, test_group, prow_dir)

    with open(testgrid_config, "w") as config_fp:
        ruamel.yaml.dump(config, config_fp,
                         Dumper=ruamel.yaml.RoundTripDumper, width=MAX_WIDTH)
        config_fp.truncate()


def move_tab(dashboard, dash_tab, prow_dir):
    """Moves a given tab to the matching prow job in prow_dir, if possible"""
    dashboard_name = dashboard["name"]
    prow_job_name = dash_tab["test_group_name"]
    prow_job_file_name = find_prow_job(prow_job_name, prow_dir)
    if prow_job_file_name == "":
        return

    # Matching file found; patch and delete
    print("Patching tab {0} in {1}".format(prow_job_name, prow_job_file_name))

    with open(prow_job_file_name, "r") as job_fp:
        prow_config = ruamel.yaml.load(job_fp,
                                       Loader=ruamel.yaml.RoundTripLoader,
                                       preserve_quotes=True)

    # For each presubmits, postsubmits, periodic:
    # presubmits -> <any repository> -> [{name: prowjob}]
    if "presubmits" in prow_config:
        for _, jobs in prow_config["presubmits"].items():
            for job in jobs:
                if prow_job_name == job["name"]:
                    job = patch_prow_job_with_tab(job, dash_tab, dashboard_name)

    # postsubmits -> <any repository> -> [{name: prowjob}]
    if "postsubmits" in prow_config:
        for _, jobs in prow_config["postsubmits"].items():
            for job in jobs:
                if prow_job_name == job["name"]:
                    job = patch_prow_job_with_tab(job, dash_tab, dashboard_name)

    # periodics -> [{name: prowjob}]
    if "periodics" in prow_config:
        for job in prow_config["periodics"]:
            if prow_job_name == job["name"]:
                job = patch_prow_job_with_tab(job, dash_tab, dashboard_name)

    # Dump ProwConfig to prowJobFile
    with open(prow_job_file_name, "w") as job_fp:
        ruamel.yaml.dump(prow_config, job_fp,
                         Dumper=ruamel.yaml.RoundTripDumper, width=MAX_WIDTH)
        job_fp.truncate()

    # delete tab
    dashboard["dashboard_tab"].remove(dash_tab)


def move_group(config, group, prow_dir):
    """Moves a given test group to the first matching prow job in prow_dir, if possible"""
    prow_job_name = group["name"]
    prow_job_file_name = find_prow_job(prow_job_name, prow_dir)
    if prow_job_file_name == "":
        return

    # Matching file found; patch and delete
    print("Patching group {0} in {1}".format(prow_job_name, prow_job_file_name))

    with open(prow_job_file_name, "r") as job_fp:
        prow_config = ruamel.yaml.load(job_fp,
                                       Loader=ruamel.yaml.RoundTripLoader,
                                       preserve_quotes=True)

    # For each presubmit, postsubmit, and periodic
    # presubmits -> <any repository> -> [{name: prowjob}]
    # An annotation must be forced, or else the testgroup will not be generated
    if "presubmits" in prow_config:
        for _, jobs in prow_config["presubmits"].items():
            for job in jobs:
                if prow_job_name == job["name"]:
                    job = patch_prow_job_with_group(job, group, force_group_creation=True)
                    break

    # postsubmits -> <any repository> -> [{name: prowjob}]
    if "postsubmits" in prow_config:
        for _, jobs in prow_config["postsubmits"].items():
            for job in jobs:
                if prow_job_name == job["name"]:
                    job = patch_prow_job_with_group(job, group)
                    break

    # periodics -> [{name: prowjob}]
    if "periodics" in prow_config:
        for job in prow_config["periodics"]:
            if prow_job_name == job["name"]:
                job = patch_prow_job_with_group(job, group)
                break

    # Dump ProwConfig to prowJobFile
    with open(prow_job_file_name, "w") as job_fp:
        ruamel.yaml.dump(prow_config, job_fp,
                         Dumper=ruamel.yaml.RoundTripDumper, width=MAX_WIDTH)
        job_fp.truncate()

    config["test_groups"].remove(group)

def assert_tab_keys(tab):
    """Asserts if a dashboard tab is able to be migrated.

    To be migratable, the annotations must only contain whitelisted keys
    AND alert_options, if present, must contain and only contain "alert_mail_to_addresses"
    """
    whitelist = ["name", "description", "test_group_name", "alert_options",
                 "num_failures_to_alert", "alert_stale_results_hours", "num_columns_recent"]

    if [k for k in tab.keys() if k not in whitelist]:
        return False

    if "alert_options" in tab:
        alert_keys = tab["alert_options"].keys()
        if len(alert_keys) != 1 or "alert_mail_to_addresses" not in alert_keys:
            return False

    return True

def assert_group_keys(group):
    """Asserts if a testgroup is able to be migrated.

    To be migratable, the group must only contain whitelisted keys
    """
    whitelist = ["name", "gcs_prefix", "alert_stale_results_hours",
                 "num_failures_to_alert", "num_columns_recent"]

    if [k for k in group.keys() if k not in whitelist]:
        return False
    return True


def find_prow_job(name, path):
    """Finds a Prow Job in a given subdirectory.

    Returns the first file that contains the named prow job
    Returns "" if there isn't one
    Dives into subdirectories
    Ignores PROW_BLACKLIST
    """
    pattern = re.compile("name: '?\"?" + name + "'?\"?$", re.MULTILINE)
    for (dirpath, _, filenames) in walk(path):
        for filename in filenames:
            if filename.endswith(".yaml") and filename not in PROW_BLACKLIST:
                for _, line in enumerate(open(dirpath + "/" + filename)):
                    for _ in re.finditer(pattern, line):
                        #print "Found %s in %s" % (name, filename)
                        return dirpath + "/" + filename
    return ""


def patch_prow_job_with_tab(prow_yaml, dash_tab, dashboard_name):
    """Updates a Prow YAML object.

    Assumes a valid prow yaml and a compatible dashTab
    Will create a new annotation or amend an existing one
    """
    if "annotations" in prow_yaml:
        # There exists an annotation; amend it
        annotation = prow_yaml["annotations"]
        if "testgrid-dashboards" in prow_yaml["annotations"]:
            # Existing annotation includes a testgrid annotation
            # The dashboard name must come first if it's a sig-release-master-* dashboard
            if dashboard_name.startswith("sig-release-master"):
                annotation["testgrid-dashboards"] = (dashboard_name
                                                     + ", "
                                                     + annotation["testgrid-dashboards"])
            else:
                annotation["testgrid-dashboards"] += (", " + dashboard_name)
        else:
            #Existing annotation is non-testgrid-related
            annotation["testgrid-dashboards"] = dashboard_name

    else:
        # There is no annotation; construct it
        annotation = {"testgrid-dashboards": dashboard_name}

    # Append optional annotations
    if ("name" in dash_tab
            and "testgrid-tab-name" not in annotation
            and dash_tab["name"] != dash_tab["test_group_name"]):
        annotation["testgrid-tab-name"] = dash_tab["name"]

    if ("alert_options" in dash_tab
            and "alert_mail_to_addresses" in dash_tab["alert_options"]
            and "testgrid-alert-email" not in annotation):
        annotation["testgrid-alert-email"] = dash_tab["alert_options"]["alert_mail_to_addresses"]

    opt_arguments = [("description", "description"),
                     ("num_failures_to_alert", "testgrid-num-failures-to-alert"),
                     ("alert_stale_results_hours", "testgrid-alert-stale-results-hours"),
                     ("num_columns_recent", "testgrid-num-columns-recent")]

    for tab_arg, annotation_arg in opt_arguments:
        if (tab_arg in dash_tab and annotation_arg not in annotation):
            # Numeric arguments need to be coerced into strings to be parsed correctly
            annotation[annotation_arg] = str(dash_tab[tab_arg])

    prow_yaml["annotations"] = annotation
    return prow_yaml


def patch_prow_job_with_group(prow_yaml, test_group, force_group_creation=False):
    """Updates a prow YAML object

    Assumes a valid prow yaml and a compatible test group
    Will amend existing annotations or create one if there is data to migrate
    If there is no migratable data, an annotation will be forced only if specified
    """
    if "annotations" in prow_yaml:
        # There exists an annotation; amend it
        annotation = prow_yaml["annotations"]
    else:
        annotation = {}

    # migrate info
    opt_arguments = [("num_failures_to_alert", "testgrid-num-failures-to-alert"),
                     ("alert_stale_results_hours", "testgrid-alert-stale-results-hours"),
                     ("num_columns_recent", "testgrid-num-columns-recent")]

    for group_arg, annotation_arg in opt_arguments:
        if (group_arg in test_group and annotation_arg not in annotation):
            # Numeric arguments need to be coerced into strings to be parsed correctly
            annotation[annotation_arg] = str(test_group[group_arg])

    if force_group_creation and "testgrid-dashboards" not in annotation:
        annotation["testgrid-create-test-group"] = "true"

    if not any(annotation):
        return prow_yaml

    prow_yaml["annotations"] = annotation
    return prow_yaml


if __name__ == '__main__':
    PARSER = argparse.ArgumentParser(
        description='Migrates Testgrid Tabs to Prow')
    PARSER.add_argument(
        '--testgrid-config',
        default='../testgrid/config.yaml',
        help='Path to testgrid/config.yaml')
    PARSER.add_argument(
        '--prow-job-dir',
        default='../config/jobs',
        help='Path to Prow Job Directory')
    ARGS = PARSER.parse_args()

    main(ARGS.testgrid_config, ARGS.prow_job_dir)
