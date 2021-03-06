#!/usr/bin/env python
"""
jenkins log analyzer failures

Usage:
    jenkinsbot.py --job_id=<job_id> --room_id=<room_Id> --jenkins_username=<acc_username> --jenkins_password=<acc_password> --jenkins_url=<job_url> --post_retries=<True|False> --influx_host=<ip_address> --influx_port=<port number> --post_success=<True|False>  --enable_jira=<True|False>
    jenkinsbot.py -h | --help
Options:
    --job_id=<job_id>                          # jenkins job id
    --room_id=<room_id                         # The spark room id to wich the results have to be posted
    --jenkins_username=<jenkins_username>      # The jenkins machine account username
    --jenkins_password=<jenkins_password>      # The password of the jenkins machine account
    --jenkins_url = <jenkins_url>              # The jenkins job url
    --post_retires=<post_retries>              # The post retries flag
    --influx_host=<influx_host>                # The influx host machine
    --influx_port=<influx_port>                # The port on which the influx is listening
    --post_success=<post_success>              # The post success flag
    --enable_jira=<create_jira>                # To Create jira.
Example:
    jenkinsbot.py --job_id='btstap' --room_id='75f570ea-8f09-3006-9b55-46e8114d85ce' --jenkins_username="XXX" --jenkins_password="XXXX" --jenkins_url='https://sqbu-jenkins-01.cisco.com:8443/job/ss4/job/SS4%20Sanity%20Gating/' --post_retries=True --influx_host='10.196.6.215' --influx_port='8086' --post_success=True  --enable_jira=False
"""
import sys
import requests
import time
import sqlite3
import datetime
import docopt
import os
import socket
import json
import kibanautil
from string import Template
from cilogparser2_gating import parselogs, set_bot_cfg, set_logger, generate_archive_file
from device_user_info import get_locus_id_new, get_spark_user_uuids, get_start_time, get_end_time, \
    populate_device_details_failed_test_case, get_meeting_id_and_domain, set_user_info_logger, \
    validate_spark_user_creation
from analyze_meeting import process_5xx_4xx_locusid, analyse_confluence
import influx_dbmanager
from errorconfig import data
from bs4 import BeautifulSoup
import spark_util
import http_requests
import cilogparser2_gating
import device_user_info
from customlogger import get_module_logger
from distutils.dir_util import copy_tree
import subprocess
from paramiko import SSHClient, config
from paramiko import AutoAddPolicy
from scp import SCPClient
from xml.dom.minidom import parse, parseString
import cookielib
import urllib
import urllib2
import base64
from objdict import ObjDict
import socket
from contextlib import closing
import config.influxdb_config as influx_config
from yattag import Doc, indent
from config import config
from spark_int_jira.jira_issue import JIRAUtil
from report_system import Reportsystem

import config.auth_mgr as auth_mgr

# spark_auth_mgr = None

BOT_CRASH_NOTIFICATION_ROOM = '99d76e60-11ee-11e6-867f-2baefd21879e'

try:
    # suppress urllib3 warning about unverified http requests
    requests.packages.urllib3.disable_warnings()
except BaseException:
    pass

from cilogparser2_gating import soft_assert_errors

bearer_token_lookup = {
    'ss4ci.gen': 'MDgwMTQ5NGUtZWViOS00NzYyLWE1ZTItOGM4NmMzYTFhNDMxZmE4MjY3OGUtYmM5',
    'sparkgit.gen': 'MDgwMTQ5NGUtZWViOS00NzYyLWE1ZTItOGM4NmMzYTFhNDMxZmE4MjY3OGUtYmM5'
}

spark_auth_mgr = auth_mgr.AuthMgr(config.AUTH_CONFIG)

errors = None


class JenkinsBot(object):
    """
    Generic class that represents a bot that monitors a given Jenkins job and reports failures in a
    given Spark room after analyzing the Jenkins log messages. Takes a configuration file -
    'config.json' as input to monitor on a given Jenkins job and post in a given Spark room.
    """

    def __init__(self, job_name, args):
        self._job_name = job_name
        self._influx_write = False
        self._cfg = None
        job_config = ObjDict()
        job_config.spark_room = args['--room_id']
        job_config.jenkins_username = args['--jenkins_username']
        job_config.jenkins_password = args['--jenkins_password']
        job_config.title = args['--job_id']
        job_config.jenkins_url = args['--jenkins_url']
        job_config.post_retries = args['--post_retries']
        job_config.influx_host = args['--influx_host']
        job_config.influx_port = args['--influx_port']
        job_config.post_success = args['--post_success']
        job_config.enable_jira = args['--enable_jira']

        self.logger = get_module_logger("jenkinsbot")
        if job_config.influx_host and self.test_influx_connection(influx_host=job_config.influx_host,
                                                                  influx_port=job_config.influx_port):
            self._influx_write = True

        job_config.bearer_token = bearer_token_lookup[job_config.jenkins_username]
        self._cfg = job_config
        # This change is for populating errors_t table (one time activity)
        # self.populate_issues_table()

    def test_influx_connection(self, influx_host, influx_port):
        influx_ping_api = 'http://{0}:{1}/ping'.format(influx_host, influx_port)
        r = requests.get(influx_ping_api)
        if r.status_code == 204:
            self.logger.info("Influx ping request succeeded")
            return True
        else:
            self.logger.info("Influx ping request failed with the status code {}".format(r.status_code))
            return False

    def generate_mats_link(self,
                           web_conf_id, start_time, end_time, domain):
        mats_link = None
        mats_env = None
        if domain is None or 'ss4' not in domain:
            mats_env = 'PROD'
        else:
            mats_env = 'INT'
        self.logger.info('mats enviornment is {}'.format(mats_env))
        if web_conf_id and mats_env == 'PROD':
            mats_link = 'https://mats.webex.com/page/meeting/confreq.jsp?reqindex=1&keyword={0}&start_time={1}&stop_time={2}'.format(
                web_conf_id, start_time, end_time)
            self.logger.info('Generated mats link for the test is {}'.format(mats_link))
        elif web_conf_id and mats_env == 'INT':
            mats_link = 'https://matsats2.webex.com/page/meeting/confreq.jsp?reqindex=1&keyword={0}&start_time={1}&stop_time={2}'.format(
                web_conf_id, start_time, end_time)
            self.logger.info('Generated mats link for the test is {}'.format(mats_link))
        return mats_link

    def is_port_available(self, host, port):
        """
        Whether the port is available
        :param host: Host Name to check
        :param port: Port to check
        :return: TRUE/FALSE
        """
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            if sock.connect_ex((host, port)) == 0:
                self.logger.info("port {} not available".format(port))
                return False
            else:
                self.logger.info("port {} available".format(port))
                return True

    def download_vcs_logs(self, url, url_opener, url_request, filter_string, vcs_log_dir):
        try:
            status = False
            reply = url_opener.open(url_request)
            data = '\n'.join(reply.readlines())
            self.logger.info('loggedin_status:' + reply.url)
            encoded_string = base64.b64encode(filter_string)
            networklog_url = 'https://' + url + '/networklog?all_text=' + encoded_string + \
                             '&exact_text=&any_text=&exclude_text=&search_option=bW9yZSBvcHRpb25z'
            req = urllib2.Request(networklog_url)
            reply = url_opener.open(req)
            data = '\n'.join(reply.readlines())
            if not 'Nothing matching' in data:
                log_file = vcs_log_dir + '/vcs_' + url + '.html'
                f = open(log_file, 'w')
                f.write(data)
                f.close()
                status = True
            else:
                self.logger.info('vcs_meeting_id no matching...')
            req = urllib2.Request('https://' + url + '/logout')
            reply = url_opener.open(req)
            self.logger.info('loggedout_status:' + reply.url)
            return status
        except Exception as e:
            self.logger.exception(e)
            return status

    def collect_vcs_logs(self, filter_string, vcs_log_dir):
        backbone_vcs_list = ['10.22.164.213', '10.22.164.212']
        alpha_vcs_list = ['128.107.201.136']
        try:
            cookies = cookielib.LWPCookieJar()
            self.logger.info('cookies' + str(cookies))
            opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cookies))
            for url in backbone_vcs_list:
                try:
                    req = urllib2.Request('https://' + url + '/login', urllib.urlencode(
                        {'username': 'admin', 'password': 'ciscotxbu', 'submitbutton': 'Login'}))
                    if self.download_vcs_logs(url, opener, req, filter_string, vcs_log_dir):
                        return
                except:
                    continue
            for url in alpha_vcs_list:
                try:
                    req = urllib2.Request('https://' + url + '/login', urllib.urlencode(
                        {'username': 'readonly', 'password': 'readonly', 'submitbutton': 'Login'}))
                    if self.download_vcs_logs(url, opener, req, filter_string, vcs_log_dir):
                        return
                except:
                    continue
        except Exception as e:
            self.logger.exception(e)

    def get_spark_auth_token(self):
        token = spark_auth_mgr.get_authorization_token()
        return token

    def revoke_auth_token(self):
        spark_auth_mgr.revoke_token()

    def download_logs_from_proxy(self, endpoint_dict, scp_dir):
        for k in endpoint_dict.keys():
            for d in endpoint_dict[k]:
                edpt_ip = d['ipAddress'].strip()
                self.logger.info("endpoint ip: " + edpt_ip)
                try:
                    xml_url = 'http://' + edpt_ip + '/getxml?location=/Configuration/Conference/' \
                                                    'DefaultCall/Protocol'
                    res = requests.get(xml_url, auth=('admin', 'cisco'))
                    if 'spark' in res.content.lower():
                        self.logger.info(edpt_ip + ' registed with Spark')
                        continue
                    xml_url = 'http://' + edpt_ip + '/getxml?location=/Status/SIP/Proxy/Address'
                    res = requests.get(xml_url, auth=('admin', 'cisco'))
                    xml_dom = parseString(res.content)
                    proxy_ip = xml_dom.getElementsByTagName("Address")[0].childNodes[0].data
                    if ':506' in proxy_ip:
                        continue
                    res = requests.get('http://' + proxy_ip, verify=False)
                    if 'Cisco Unified Communications Manager' in res.content:
                        ssh = SSHClient()
                        ssh.set_missing_host_key_policy(AutoAddPolicy())
                        ssh.connect('ftp-sjcvtg-011.cisco.com', username='deteam',
                                    password='deteam')
                        scp = SCPClient(ssh.get_transport())
                        (stdin, stdout, stderr) = ssh.exec_command('ls -rt /newlogs/')
                        if proxy_ip in str(stdout.readlines()):
                            (stdin, stdout, stderr) = ssh.exec_command(
                                'ls -rt /newlogs/' + proxy_ip + '/ | tail -n1')
                            res = [i for i in stdout.readlines()]
                            cucm_log_dir = res[0].strip()
                            self.logger.info("cucm_log_dir: " + cucm_log_dir)
                            cmd = 'ls -rt /newlogs/' + proxy_ip + '/' + \
                                  cucm_log_dir + '/cm/trace/ccm/sdl | tail -n2'
                            (stdin, stdout, stderr) = ssh.exec_command(cmd)
                            res = [i for i in stdout.readlines()]
                            for f in res:
                                cucm_log_file = '/newlogs/' + proxy_ip + '/' + \
                                                cucm_log_dir + '/cm/trace/ccm/sdl/' + f.strip()
                                self.logger.info("cucm_log_file " + cucm_log_file)
                                scp.get(cucm_log_file, local_path=scp_dir)
                        else:
                            self.logger.info(proxy_ip + 'not registed with Alpha CUCM')
                except Exception as ex:
                    self.logger.exception(ex)

    def process_failed_success_with_retries_build(
            self, datetime_obj, job_msg_result, parse_logs_result, jobid, joburl, env, influx_client):
        msg = 'Last completed ' + \
              self._cfg['title'] + ' build: [' + str(jobid) + '](' + joburl + ')\n\n'
        msg += 'Build status: **' + job_msg_result + '**\n\n'
        report_dict = {}
        failures_str = ''
        meeting_number = None
        webex_conf_id = None
        endpoint_dict = {}
        call_summary_link = None
        mats_link = None
        kibana_link = None
        summary_text = ''
        output_dict = {}
        analyzer_input_data = {}

        if parse_logs_result:
            msg += 'Total number of tests: ' + str(parse_logs_result[2]) + '\n\n'
            msg += 'Pass count: ' + str(parse_logs_result[2] - parse_logs_result[3]) + '\n\n'
            msg += 'Fail count: ' + str(parse_logs_result[3]) + '\n\n'
            if self._cfg['post_retries'] == "True":
                msg += 'Issues (including retries):\n\n'
            else:
                msg += 'Issues :\n\n'
            dashboard_file_data = {}
            dashboard_file_data['passed'] = str(parse_logs_result[2])
            dashboard_file_data['failed'] = str(parse_logs_result[3])
            dashboard_file_data['test_failures'] = {}
            success_status_val = 0
            jira_message = ''
            if job_msg_result == 'SUCCESS with Retries':
                success_status_val = 1

            try:
                if self._influx_write is True:
                    if not influx_dbmanager.write_data_to_job_results_t(influx_client,
                                                                        jobid, parse_logs_result[3], self._job_name,
                                                                        success_status_val):
                        self.logger.exception(
                            "Data for the successful test {} not written in influx database".format(jobid))
                        self.logger.info("failues count" + str(parse_logs_result[4]))
                for joberror, count in parse_logs_result[4].items():
                    error_code, error_desc = self.find_Value(data, joberror.lower())
                    if not influx_dbmanager.write_data_to_issues_counts(influx_client,
                                                                        error_code, error_desc, count, self._job_name):
                        self.logger.exception(
                            "Data for the successful test {} not written in "
                            "influx database".format(joberror))
                else:
                    self.logger.info("Influx write is false. Not writing to the influx database")
                if job_msg_result == 'SUCCESS with Retries' and self._cfg['post_retries'] == "False":
                    self.logger.info(
                        "build {} passed SUCCESS with Retries".format(joburl))
                    return
            except BaseException as e:
                self.logger.exception("Hit exception while trying to write to influx ", e)
                spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM, e, self._cfg['bearer_token'])
            try:
                r2 = requests.get(
                    joburl +
                    '/artifact/test-output/emailable-report.html',
                    auth=(
                        self._cfg['jenkins_username'],
                        self._cfg['jenkins_password']),
                    verify=False)
            except requests.ConnectionError as ex:
                if isinstance(ex.message, str):
                    msg = 'Error occurred while fetching log file to extract other details:' + \
                          self.logger.exception(msg)
                else:
                    msg = 'Connection Error occurred while fetching log file to extract other ' \
                          'details'
                    self.logger.exception(msg)
                return
            except BaseException:
                self.logger.exception(
                    'Unknown error occurred while fetching log file to extract other details')
                return
            # start writing into results file
            job_file_dir = '/jenkins_log_analyzer/src/' + self._job_name
            if not os.path.exists(job_file_dir):
                os.mkdir(job_file_dir)
            run_dir = self._job_name + '/' + self._job_name + str(datetime_obj.year) + \
                      str(datetime_obj.month) + str(datetime_obj.day) + '_' + str(jobid)
            self.logger.info("Run dir job name {0}, job id {1} ".format(self._job_name, str(jobid)))
            os.mkdir(run_dir)
            log_dir = '/jenkins_log_analyzer/src/' + run_dir
            report_dict['reports'] = []
            report_dict['title'] = self._cfg['title'] + '' + str(jobid)
            job_file = open('a.txt', 'w')
            extracted_kibana_results = {}
            self.logger.info('the values from parse output are %s' % parse_logs_result[8].items())
            for test_case, job_issue_list in parse_logs_result[8].items():
                message_tuple = ''
                for item in job_issue_list:
                    message_tuple += item[1] + ',' + '\t'
                message_tuple += '\n'
                jira_message += test_case + '\t' + message_tuple + '\n'
                issue_count = 0
                # Iterate for all the failures in same testcase
                extracted_kibana_results[test_case] = {}
                extracted_kibana_results[test_case]["issues"] = {}
                failure_reason = ''
                for failure in job_issue_list:
                    failure_reason += failure[1] + ','
                failure_reason = failure_reason[:-1]
                dashboard_file_data['test_failures'][test_case] = failure_reason
                for issue in job_issue_list:
                    try:
                        extracted_kibana_results[test_case]["issues"][issue] = {}
                        extracted_kibana_results[test_case]["issues"][issue]["queries"] = []
                        output_dict = {}
                        issue_count = issue_count + 1
                        meeting_number, domain = get_meeting_id_and_domain(r2, test_case, issue_count)
                        token = self.get_spark_auth_token()
                        locus_id = get_locus_id_new(r2, test_case, issue_count)
                        uuidList = get_spark_user_uuids(r2, test_case, issue_count)
                        start_time = get_start_time(r2, test_case, issue_count)
                        ecp_ip = device_user_info.get_ecp_ip(r2, test_case, issue_count)
                        self.logger.info("The ecp Ip is {}".format(ecp_ip))
                        call_history = device_user_info.get_call_history(r2, test_case, issue_count)
                        end_time = get_end_time(r2, test_case, issue_count)

                        self.logger.info(
                            "Meeting number " +
                            str(meeting_number) +
                            "locus_id " +
                            str(locus_id) +
                            "Start time " +
                            str(start_time) +
                            "endtime " +
                            str(end_time))

                        kibana_locus_id, webex_conf_id = kibanautil.get_locus_and_webex_conf_ids(token,
                                                                                                 meeting_number,
                                                                                                 locus_id,
                                                                                                 start_time,
                                                                                                 end_time, env)
                        if locus_id is None:
                            locus_id = kibana_locus_id
                        kibana_link = kibanautil.generate_kibana_link(meeting_number, locus_id, start_time,
                                                                      end_time, env)

                        endpoint_dict = populate_device_details_failed_test_case(
                            r2, test_case, issue_count)
                    except Exception as e:
                        self.logger.exception(e.message)
                    try:
                        try:
                            if locus_id and start_time and end_time:
                                if domain is None or domain == 'PROD':
                                    call_summary_link = 'http://calliope-debug.koalabait.com/summary?id_type=lid&id_value=' + \
                                                        locus_id + '&start_time=' + \
                                                        start_time + '&stop_time=' + end_time
                                else:
                                    call_summary_link = 'http://calliope-debug-load.koalabait.com/summary?id_type=lid&id_value=' + \
                                                        locus_id + '&start_time=' + \
                                                        start_time + '&stop_time=' + end_time
                            else:
                                call_summary_link = None
                                summary_text = ''
                        except (requests.ConnectionError, ValueError, NameError, TypeError) as err:
                            self.logger.exception(err.args)
                        if call_summary_link:
                            res1 = requests.get(call_summary_link)
                            if res1.status_code == 200:
                                soup = BeautifulSoup(res1.text, 'html.parser')
                                summary_text = soup.find(
                                    id='call-info-full-ta-0').string
                    except BaseException:
                        self.logger.exception('Failed to get call summary')
                    try:
                        mats_link = self.generate_mats_link(webex_conf_id, start_time, end_time, domain)
                    except (requests.ConnectionError, ValueError) as err:
                        self.logger.exception(err.args)
                    self.revoke_auth_token()

                    output_dict['start_time'] = start_time
                    output_dict['end_time'] = end_time
                    output_dict['meeting_number'] = meeting_number
                    if locus_id is None:
                        spark_user_creation_status = validate_spark_user_creation(
                            r2, test_case)
                        if spark_user_creation_status:
                            self.logger.info(
                                "Locus id is absent, spark user creation succeeded")
                            output_dict['locus_id'] = None
                            output_dict['spark_user_creation_status'] = 'Passed'
                        else:
                            self.logger.info(
                                "Locus id is absent, spark user creation Failed or Not Applicable")
                            output_dict['locus_id'] = None
                            output_dict['spark_user_creation_status'] = 'Failed or Not Applicable'
                    else:
                        output_dict['locus_id'] = locus_id
                        output_dict['spark_user_creation_status'] = 'Passed'
                    output_dict['test_case'] = test_case
                    output_dict['webex_conf_id'] = webex_conf_id
                    output_dict['uuidList'] = uuidList
                    output_dict['participant_info'] = endpoint_dict
                    output_dict['call_summary_link'] = call_summary_link
                    output_dict['mats_link'] = mats_link
                    output_dict['kibana_link'] = kibana_link
                    output_dict['call_summary'] = summary_text
                    output_dict['call_history'] = str(call_history)

                    report_dict['reports'].append(output_dict)
                    error_category = data.get(issue[1].lower(), None)
                    analyzer_input_data['error_code'] = error_category
                    analyzer_input_data['locus_id'] = locus_id
                    analyzer_input_data['meeting_number'] = meeting_number
                    analyzer_input_data['domain'] = domain
                    analyzer_input_data['ecp_ip_address'] = ecp_ip
                    analyzer_input_data['start_time'] = start_time
                    analyzer_input_data['end_time'] = end_time
                    analyzer_input_data_copy = analyzer_input_data.copy()
                    for key in analyzer_input_data_copy:
                        if analyzer_input_data_copy[key] is None or analyzer_input_data_copy[key] == '':
                            del analyzer_input_data[key]

                    kibana_analysis_result = None
                    # try:
                    #     kibana_analysis_result = kibanautil.analyze_the_data_to_detect_issue(analyzer_input_data)
                    # except BaseException as e:
                    #     self.logger.error(e, exc_info=True)
                    # self.logger.info("Kibana analysis result {}".format(kibana_analysis_result))
                    # if kibana_analysis_result:
                    #     extracted_kibana_results[test_case]["issues"][issue]["queries"] = kibana_analysis_result["executed"]

                if endpoint_dict:
                    self.download_logs_from_proxy(endpoint_dict, log_dir)
                if meeting_number and domain:
                    filter_string = meeting_number + '@' + domain
                    self.logger.info("filter_string:" + filter_string)
                    self.collect_vcs_logs(filter_string, log_dir)
                # concatenate all the issue strings
                for job_issue_tuple in job_issue_list:
                    issues_str = job_issue_tuple[1]
                    failures_str += '* ' + test_case + ': ' + issues_str + '\n\n'
            self.logger.info("Extracted Kibana Info :\n {}".format(extracted_kibana_results))
            # post the message into the Spark room
            if failures_str:
                msg += failures_str
            else:
                msg += 'Jenkins log not available for analysis!'
            html_content = self.generate_kibana_html_reports(extracted_kibana_results)
            kibana_html_file = os.path.join(run_dir, "kibana_results.html")
            emailable_report_html = os.path.join(run_dir, 'emailable_report.html')
            with open(kibana_html_file, 'w') as kibana_file:
                kibana_file.write(html_content)
            failure_build_html_content = self.generate_html_report(report_dict)
            complete_html_file = os.path.join(run_dir, "Job_Debug_info.html")
            with open(complete_html_file, 'w') as build_details_file:
                build_details_file.write(failure_build_html_content)
            response_text_str = r2.text.encode('utf-8')
            with open(emailable_report_html, 'w') as emailable_report_file:
                emailable_report_file.write(response_text_str)
            self.logger.info('Debug info collected are: ' + str([report_dict]))
            # self.generate_html_report(
            #   run_dir + '/' + self._job_name + '_' + str(jobid) + '_details.html', report_dict)

            archive_file_name = str(run_dir).split('/')[1]
            self.logger.info("The run dir is {}".format(run_dir))
            self.logger.info("The archive file name is {}".format(archive_file_name))

            archive_status, archive_file = generate_archive_file(
                archive_file_name, 'gztar', job_file_dir)
            if archive_status:
                self.logger.info("Successfully generated the archive file {}".format(archive_file))
            else:
                spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM,
                                              "Could not generate archive file {0} for the job file dir {1}".format(
                                                  archive_file_name, job_file_dir))

            cpRegion = os.getenv('cpRegion')
            webexDomain = os.getenv('webexDomain')
            self.logger.info("Uploading data to report system")
            dashboard_url_data = {
                'projectCode': self._cfg['title'],
                'buildNumber': jobid,
                'type': 'text',
                'formatType': 'text'
            }
            self.logger.info("Dashboard file data for the report system is {}".format(str(dashboard_file_data)))
            self.logger.info("Dashboard url data for the report system is  {}".format(str(dashboard_url_data)))
            reportsystem = Reportsystem()
            upload_status = reportsystem.upload_to_report_system(dashboard_url_data, dashboard_file_data)
            self.logger.info("Upload status of the data to the report system is {}".format(str(upload_status)))
            if cpRegion is not None and webexDomain is not None:
                msg += 'Job Executed on CP Region {} and site is {}'.format(cpRegion, webexDomain)
            post_res = False
            retry_count = 0
            if self._cfg['enable_jira'] == 'True':
                try:
                    with open('./spark_int_jira/spark_config.json', 'r') as f:
                        jira_data = json.loads(f.read())
                        oauth_dict = jira_data['config']['OAUTH_DICT']
                        jira_url = jira_data['config']['JIRA_URL']
                    with open('./spark_int_jira/key', 'r') as f:
                        oauth_dict['key_cert'] = f.read()
                    issuetype = jira_data['config']['ISSUE_TYPE']
                    found_data = jira_data['config']['FOUND']
                    label_data = jira_data['config']['LABELS']
                    if 'MFAUTO'.lower() in self._job_name.lower():
                        project_key = 'SPARK'
                        component = jira_data['config']['Components']['SPARK']
                    else:
                        project_key = 'WEBEX'
                        component = jira_data['config']['Components']['WEBEX']
                    jira_util = JIRAUtil(jira_url, project_key, oauth_dict=oauth_dict)
                    jira_util.connect_jira()
                    new_issue = jira_util.create_jira_issue(self._job_name, self._cfg['jenkins_url'],
                                                            jira_message, found_data, label_data, component, issuetype)
                    if type(new_issue) == str:
                        spark_util.post_spark_room(self._cfg['spark_room'], new_issue,
                                                   self._cfg['bearer_token'])
                    else:
                        issue_url = jira_util.url + '/browse/' + new_issue.key
                        msg += 'JIRA URL :' + '\t' + issue_url + '\n'
                except Exception as e:
                    spark_util.post_spark_message(self._cfg['spark_room'],
                                                  'Unable to generate the jira message',
                                                  self._cfg['bearer_token'])
                    self.logger.exception(str(e))

            try:

                while post_res is False and retry_count < 5:
                    if self._cfg['post_retries'] == "True" and job_msg_result == 'SUCCESS with Retries':
                        post_res = spark_util.post_spark_message(self._cfg['spark_room'], msg,
                                                                 self._cfg['bearer_token'],
                                                                 file_name=archive_file,
                                                                 file_path=archive_file,
                                                                 file_type='application/gzip')

                    elif self._cfg['post_retries'] == "False" and job_msg_result == 'SUCCESS with Retries':
                        retry_count += 1
                        continue
                    elif job_msg_result == 'FAILURE':
                        post_res = spark_util.post_spark_message(self._cfg['spark_room'], msg,
                                                                 self._cfg['bearer_token'],
                                                                 file_name=archive_file,
                                                                 file_path=archive_file,
                                                                 file_type='application/gzip')

                        self.logger.info("The result of the post is {}".format(post_res))

                    retry_count += 1
            except BaseException as e:
                spark_util.post_spark_message(self._cfg['spark_room'], msg,
                                              self._cfg['bearer_token'])
            return report_dict
        else:
            try:
                if self._influx_write:
                    if not influx_dbmanager.write_data_to_job_results_t(influx_client,
                                                                        jobid, 1, self._job_name, 0):
                        self.logger.error('Could not write data for no log case')
            except BaseException as e:
                self.logger.exception("Hit exception while to write to influx", e)
                spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM, e, self._cfg['bearer_token'])
        return output_dict

    def process_aborted_build(self, jobid, joburl, title, influx_client):
        abort_msg = 'The following job of {} has aborted {}'.format(title, joburl)
        cpRegion = os.getenv('cpRegion')
        webexDomain = os.getenv('webexDomain')
        if cpRegion is not None and webexDomain is not None:
            msg = 'Job Executed on cpRegion {} and site {}'.format(cpRegion, webexDomain)
            spark_util.post_spark_message(self._cfg['spark_room'], msg, self._cfg['bearer_token'])
        spark_util.post_spark_message(self._cfg['spark_room'], abort_msg,
                                      self._cfg['bearer_token'])
        try:
            if self._influx_write:
                self.logger.info("Inserting job result for aborted case")
                if not influx_dbmanager.write_data_to_job_results_t(influx_client,
                                                                    jobid, 0, self._job_name, success_status=0,
                                                                    aborted_status=1):
                    self.logger.exception("Data for the aborted test {} not written "
                                          "into the influx database".format(jobid))
        except BaseException as e:
            self.logger.exception("Hit exception while to write to influx", e)
            spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM, e, self._cfg['bearer_token'])
        else:
            self.logger.info("Influx write is false. No data written to the database")

    def process_success_build(self, jobid, joburl, title, influx_client, total_count):
        cpRegion = os.getenv('cpRegion')
        webexDomain = os.getenv('webexDomain')
        if cpRegion is not None and webexDomain is not None:
            msg = 'Job Executed on cpRegion {}  and site {}'.format(cpRegion, webexDomain)
            spark_util.post_spark_message(self._cfg['spark_room'], msg, self._cfg['bearer_token'])
        self.logger.info("Uploading data to report system")
        dashboard_url_data = {
            'projectCode': self._cfg['title'],
            'buildNumber': jobid,
            'type': 'text',
            'formatType': 'text'
        }
        self.logger.info("Dashboard url data is {}".format(str(dashboard_url_data)))
        dashboard_file_data = {
            'passed': total_count,
            'failed': 0
        }
        self.logger.info("Dashboard file data is {}".format(str(dashboard_file_data)))
        reportsystem = Reportsystem()
        upload_status = reportsystem.upload_to_report_system(dashboard_url_data, dashboard_file_data)
        self.logger.info("Upload status to report system is {}".format(str(upload_status)))
        if self._cfg["post_success"] == "True":
            success_msg = 'Hey All, build [{}]({}) of {} has passed successfully'.format(jobid, joburl, title)
            spark_util.post_spark_message(self._cfg['spark_room'], success_msg,
                                          self._cfg['bearer_token'])
        try:
            if self._influx_write:
                self.logger.info("Inserting job result for passed case")
                if not influx_dbmanager.write_data_to_job_results_t(influx_client,
                                                                    jobid, 0, self._job_name, success_status=1):
                    self.logger.exception("Data for the successful test {} not written"
                                          " into the influx database".format(jobid))
        except BaseException as e:
            self.logger.exception("Hit exception while to write to influx", e)
            spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM, e, self._cfg['bearer_token'])
        else:
            self.logger.info("Influx write is false. No data written to the database")

    def process_jenkins_build(self):
        """
        Process a given build - figure out issues, post in the Spark room.
        :param build_number: Jenkins build number that has to be processed.
        :return: None
        """

        title = self._cfg['title']
        if title == 'ss4gating':
            env = "INT"
        else:
            env = "PROD"

        last_build_url = self._cfg['jenkins_url']

        if self._cfg.influx_host:
            influx_host = self._cfg.influx_host
        else:
            influx_host = influx_config.INFLUXDBIP

        if self._cfg.influx_port:
            influx_port = self._cfg.influx_port
        else:
            influx_port = influx_config.PORT

        influx_client = influx_dbmanager.get_influx_client(influx_host,
                                                           influx_port,
                                                           influx_config.USERNAME,
                                                           influx_config.PASSWORD,
                                                           influx_config.DATABASENAME)
        try:
            data = http_requests.get(
                last_build_url +
                '/api/json?depth=1',
                auth=(
                    self._cfg['jenkins_username'],
                    self._cfg['jenkins_password']))
        except BaseException:
            self.logger.exception(
                'Unknown error occurred while fetching job details in \'process_jenkins_build\'')
            return
        if data:
            # Fetch job details
            job_number = data.json()['number']
            joburl = data.json()['url']
            datetime_obj = datetime.datetime.fromtimestamp(
                data.json()['timestamp'] / 1000.0)
            timestamp = str(datetime_obj)
            jobresult = data.json()['result']
            self.logger.info(
                'Latest job:(' +
                str(job_number) +
                ',' +
                joburl +
                ',' +
                timestamp +
                ',' +
                jobresult +
                ')')
            self.logger.info("details does not exists")
            set_bot_cfg(self._cfg)
            set_logger(self.logger)
            parse_logs_result = parselogs(
                self._job_name, str(job_number), str(job_number))
            self.logger.info('the result for parse log results is %s' % parse_logs_result)
            global errors
            errors = soft_assert_errors()
            if title == 'meeting_launch_duration':
                meeting_launch_time = cilogparser2_gating.get_launch_time(joburl, auth=(self._cfg['jenkins_username'],
                                                                                        self._cfg['jenkins_password']))
                self.logger.info("The meeting launch time is {}".format(meeting_launch_time))
                if meeting_launch_time:
                    if meeting_launch_time in range(4):
                        category = 1
                    elif meeting_launch_time in range(4, 10):
                        category = 2
                    elif meeting_launch_time > 10:
                        category = 3
                try:
                    if self._influx_write:
                        self.logger.info("Inserting job result for passed case")
                        if not influx_dbmanager.write_meeting_data(influx_client, title, meeting_launch_time, category):
                            self.logger.exception("Data for the successful test {} not written"
                                                  " into the influx database".format(title))
                except BaseException as e:
                    self.logger.exception("Hit exception while to write to influx", e)
                    spark_util.post_spark_message(BOT_CRASH_NOTIFICATION_ROOM, e, self._cfg['bearer_token'])
                else:
                    self.logger.info("Influx write is false. No data written to the database")

            if parse_logs_result and parse_logs_result[3] == 0 and parse_logs_result[9] == 0:
                job_msg_result = 'SUCCESS'
            elif parse_logs_result and parse_logs_result[3] == 0 and parse_logs_result[9] > 0:
                job_msg_result = 'SUCCESS with Retries'
            elif jobresult == 'ABORTED':
                job_msg_result = 'ABORTED'
            elif parse_logs_result and jobresult == 'FAILURE':
                job_msg_result = 'FAILURE'
            else:
                job_msg_result = 'UNDETERMINED'
            undetermined_msg = 'The build {} has failed/aborted but could not' \
                               ' find the report for this run'.format(last_build_url)
            self.logger.info("The job result is {}".format(job_msg_result))

            if parse_logs_result and len(parse_logs_result[8].keys()) == 1 and 'afterSuite' in parse_logs_result[
                8].keys():
                job_msg_result = 'SUCCESS'
            # Post in Spark room if there is a failure
            if job_msg_result != 'SUCCESS' and jobresult != 'ABORTED':
                report_dict = self.process_failed_success_with_retries_build(
                    datetime_obj, job_msg_result, parse_logs_result, job_number, joburl, env, influx_client)
            elif job_msg_result != 'SUCCESS' and jobresult == 'ABORTED':
                self.process_aborted_build(job_number, joburl, title, influx_client)
            elif job_msg_result == 'SUCCESS':
                self.process_success_build(job_number, joburl, title, influx_client, parse_logs_result[2])

            if job_msg_result == 'UNDETERMINED':
                spark_util.post_spark_message(self._cfg['spark_room'],
                                              undetermined_msg,
                                              self._cfg['bearer_token'])

    def populate_issues_table(self, influx_client):
        """
         populating the errors_t table
         Need to Run Manually if there are updates in error_code dictionary
        """
        successful_tests = [[data[key], key] for key in data]
        for test_detail in successful_tests:
            if not influx_dbmanager.write_data_to_errorcode_desc_t(influx_client,
                                                                   test_detail[0], test_detail[1]):
                self.logger.exception(
                    "Data for the successful test {} not written"
                    " in influx database".format(
                        test_detail[1]))
                spark_util.post_spark_message(
                    BOT_CRASH_NOTIFICATION_ROOM,
                    'Data not writte into the influx database',
                    self._cfg['bearer_token'])

    def generate_kibana_html_reports(self, kibana_data):
        """
        It generates html report for kibana results
        :param kibana_data: Data to be used in generating html
        :return: html data
        """
        doc, tag, text, line = Doc().ttl()
        doc.asis("<!DOCTYPE html>")
        with tag('html'):
            with tag('head'):
                doc.asis('<meta charset="utf-8">')
                doc.asis('<meta name="viewport" content="width=device-width, initial-scale=1">')
                with tag('title'):
                    text()
                doc.asis('<link rel="stylesheet" '
                         'href="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/css/bootstrap.min.css" '
                         'integrity="sha384-Gn5384xqQ1aoWXA+058RXPxPg6fy4IWvTNh0E263XmFcJlSAwiGgFAW/dAiS6JXm" '
                         'crossorigin="anonymous">')
                doc.asis('<script src="https://code.jquery.com/jquery-3.2.1.slim.min.js" '
                         'integrity="sha384-KJ3o2DKtIkvYIK3UENzmM7KCkRr/rE9/Qpg6aAZGJwFDMVNA/GpGFF93hXpG5KkN" '
                         'crossorigin="anonymous"></script>')
                doc.asis('<script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.12.9/umd/popper.min.js" '
                         'integrity="sha384-ApNbgh9B+Y1QKtv3Rn7W3mgPxhU9K/ScQsAP7hUibX39j7fakFPskvXusvfa0b4Q" '
                         'crossorigin="anonymous"></script>')
                doc.asis('<script src="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/js/bootstrap.min.js" '
                         'integrity="sha384-JZR6Spejh4U02d8jOt6vLEHfe/JQGiRRSQQxSfFWpi1MquVdAyjUar5+76PVCmYl" '
                         'crossorigin="anonymous"></script>')
            doc.asis('<body>')
            with tag('div', klass="container"):
                for test_case in kibana_data:
                    with tag('table', klass='table table-responsive-lg table-hover table-bordered table-striped small'):
                        with tag('thead', klass='thead-dark'):
                            with tag('tr'):
                                line('th', test_case,
                                     klass='text-center', colspan='3', style="font-size:25px;")
                            with tag('tr', style="font-size:20px"):
                                line('th', '#')
                                line('th', 'Issue Details', colspan='2', klass="text-center")
                        with tag('tbody'):
                            issue_count = 1
                            for issue in kibana_data[test_case]["issues"]:
                                with tag('tr'):
                                    with tag('td', klass='bg-dark text-white border border-dark font-weight-bold'):
                                        text(issue_count)
                                    with tag('td', colspan="2", klass="bg-light"):
                                        with tag('table',
                                                 klass='table table-responsive-lg table-hover table-bordered '
                                                       'table-striped'
                                                       ' small'):
                                            with tag('thead', klass='thead-dark'):
                                                with tag('tr'):
                                                    line('th', issue[1],
                                                         klass='text-center', colspan='3', style="font-size:15px;")
                                                with tag('tr'):
                                                    line('th', '#')
                                                    line('th', 'Query String')
                                                    line('th', 'Query Hits')
                                            with tag('tbody'):
                                                if len(kibana_data[test_case]["issues"][issue]["queries"]) != 0:
                                                    query_count = 1
                                                    for query_exec in \
                                                            kibana_data[test_case]["issues"][issue]["queries"]:
                                                        with tag('tr'):
                                                            with tag('td',
                                                                     klass='bg-dark text-white '
                                                                           'border border-dark font-weight-bold'):
                                                                text(query_count)
                                                            with tag('td', klass="bg-info font-weight-bold"):
                                                                text(query_exec['query_string'])
                                                            with tag('td'):
                                                                value = ''
                                                                if len(query_exec['hits']) != 0:
                                                                    for hit in query_exec['hits']:
                                                                        value += "<br>{}".format(hit)
                                                                else:
                                                                    value = "NA"
                                                                text(value)
                                                        query_count += 1
                                                else:
                                                    with tag('tr'):
                                                        with tag('td', klass='bg-warning font-weight-bold text-center',
                                                                 colspan="3"):
                                                            text(
                                                                "No queries are available "
                                                                "to execute for this type of issue.")
                                issue_count += 1

            doc.asis('</body>')
        return indent(doc.getvalue())

    def generate_html_report(self, report_dict):
        """
        This will generate html file using Dict report_dict
        param html_file_path: html file name with path
        param report_dict:  this dict contains title and all values meeting id,
                            locus id, etc.., as a list of dict in report_dict
                            to generate html file
        """
        doc, tag, text, line = Doc().ttl()
        doc.asis('<!DOCTYPE html>')
        with tag('html'):
            with tag('head'):
                doc.asis('<meta charset="utf-8">')
                with tag('title'):
                    text("HTML_report")
                doc.asis('<link rel="stylesheet" '
                         'href="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/css/bootstrap.min.css" '
                         'integrity="sha384-Gn5384xqQ1aoWXA+058RXPxPg6fy4IWvTNh0E263XmFcJlSAwiGgFAW/dAiS6JXm" '
                         'crossorigin="anonymous">')
                doc.asis('<script src="https://code.jquery.com/jquery-3.2.1.slim.min.js" '
                         'integrity="sha384-KJ3o2DKtIkvYIK3UENzmM7KCkRr/rE9/Qpg6aAZGJwFDMVNA/GpGFF93hXpG5KkN" '
                         'crossorigin="anonymous"></script>')
                doc.asis('<script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.12.9/umd/popper.min.js" '
                         'integrity="sha384-ApNbgh9B+Y1QKtv3Rn7W3mgPxhU9K/ScQsAP7hUibX39j7fakFPskvXusvfa0b4Q" '
                         'crossorigin="anonymous"></script>')
                doc.asis('<script src="https://maxcdn.bootstrapcdn.com/bootstrap/4.0.0/js/bootstrap.min.js" '
                         'integrity="sha384-JZR6Spejh4U02d8jOt6vLEHfe/JQGiRRSQQxSfFWpi1MquVdAyjUar5+76PVCmYl" '
                         'crossorigin="anonymous"></script>')
            doc.asis('<body>')
            with tag('div', klass="failed_testcases"):
                line('p', report_dict["title"], klass='text-center text-info font-weight-bold bg-dark',
                     style='font-size:35px')
                for tc in report_dict["reports"]:
                    with tag('table', klass='table-bordered border border-dark', cellpadding="2"):
                        with tag('tr'):
                            line('th', 'MEETING DETAILS', klass='font-weight-bold bg-dark text-light',
                                 style="font-size:22px",
                                 colspan="2")
                        with tag('tr'):
                            line('td', 'Test Case', klass='text-dark bg-warning', style="font-size:17px width:220px")
                            line('td', tc['test_case'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Meeting Number', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['meeting_number']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                line('td', tc['meeting_number'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Locus ID', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['locus_id']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                line('td', tc['locus_id'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Spark User Creation Status', klass='text-dark bg-warning',
                                 style='font-size:17px')
                            line('td', tc['spark_user_creation_status'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Start time of job', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['start_time']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                line('td', tc['start_time'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'End time of job', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['end_time']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                line('td', tc['end_time'], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'WebEx Conference ID', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['webex_conf_id']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                webex_conf_id = tc['webex_conf_id']
                                line('td', webex_conf_id, klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'MATS Link', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['mats_link']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                with tag('td'):
                                    line('a', 'MATS Link', href=tc['mats_link'], klass='text-dark',
                                         style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Kibana Link', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['kibana_link']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                with tag('td'):
                                    line('a', 'Kibana Link', href=tc['kibana_link'], klass='text-dark',
                                         style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Participant Info ', klass='text-dark bg-warning', style='font-size:17px')
                            j = tc['test_case']
                            k = tc["participant_info"]
                            if not bool(k):
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                with tag("td"):
                                    with tag('table', klass='table-bordered border border-dark'):
                                        with tag("tr"):
                                            line("th", "IP Address")
                                            line("th", "Endpoint Name")
                                        for l in k[j]:
                                            with tag('tr'):
                                                line('td', l["ipAddress"], klass='text-dark', style='font-size:17px')
                                                line('td', l["name"], klass='text-dark', style='font-size:17px')
                        with tag('tr'):
                            line('td', 'Call Summary Link', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['call_summary_link']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                with tag('td'):
                                    line('a', 'Call_Summary Link', href=tc['call_summary_link'],
                                         klass='text-dark font-size:17px')
                        with tag('tr'):
                            line('td', 'Call History', klass='text-dark bg-warning', style='font-size:17px')
                            if not tc['call_history']:
                                line('td', 'Not Applicable', klass='text-dark', style='font-size:17px')
                            else:
                                line('td', tc['call_history'], klass='text-dark', style='font-size:17px')

                        with tag('tr'):
                            line('td', 'Call Summary', klass='text-dark bg-warning', style='font-size:17px')
                            with tag('td', klass='text-dark', style='font-size:17px'):
                                if tc['call_summary'] == '':
                                    text('Not Available')
                                elif tc['call_summary'] is None:
                                    text('Not Available')
                                else:
                                    line('pre', tc['call_summary'])
            doc.asis('</body>')
        return indent(doc.getvalue())

    def find_Value(self, dict, lookup):
        code = '150'
        desc = 'Other Reasons'.lower()
        for k, v in dict.iteritems():
            if lookup in k:
                desc, code = k, v
                break
        return code, desc


if __name__ == "__main__":
    args = docopt.docopt(__doc__)
    job_id = args['--job_id']
    bot = JenkinsBot(job_id, args)
    bot.process_jenkins_build()

