from  pexpect import *
import sys
import pexpect
# #

child = pexpect.spawn('ssh -o StrictHostKeyChecking=no ocp@10.241.11.96', timeout=10)
#child.logfile = open("/Users/sgajjela/PycharmProjects/test/venv/mylog", "w")
child.logfile = sys.stdout
child.expect (r'.*Password:.*', timeout=50)
#print'helloo123456' + str(child.read())
child.sendline("0cpAdm1nIsN1ce!")
child.expect(pexpect.EOF)
#print child.read()
import time
time.sleep(3)
#child.expect(r'.*]*')
print ('hi')
#child.interact()
child.sendline("uptime")
child.expect(r'.*]*')
print'helloo' + str(child.readlines())





# import paramiko
# from paramiko import SSHClient, config ,client
# import paramiko
# from paramiko_expect import SSHClientInteraction
# import time
#
# ssh = paramiko.SSHClient()
# ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
# ssh.connect("10.241.11.96", username="ocp", password="0cpAdm1nIsN1ce!")
# chan = ssh.invoke_shell()
# chan.send('ls')
# time.sleep(1)
# bufsize = 1024
# output = ""
# while True:
#         print "hello"
#         if chan.recv_exit_status():
#             data = chan.recv(bufsize).decode('ascii')
#             output += data
#
#         if chan.recv_exit_status():
#             break
#
# print output
#stdin, stdout, stderr = ssh.exec_command("ls -l")
# print(stdout.readlines())
# transport = ssh.get_transport()
# session = transport.open_session()
# stdin, stdout, stderr= session.exec_command('cd /etc')
#
# print(stdout.readlines())
# ssh_connection.close()
# interact = SSHClientInteraction(ssh, timeout=10)
# interact.send('mpstat -P ALL')
# cmd_output_uname = interact.current_output
# interact.expect(r'.*]*')
# cmd_output_uname_clean = interact.current_output_clean
# print "the out put clean  is " + cmd_output_uname_clean
# print"theout is with out clean is" + cmd_output_uname
# #print 'the help out' +help(interact)
# cmd_output_uname = interact.current_output
# interact.send('ls')
#
#
# print "the out pur is " + cmd_output_uname
# cmd="mpstat -P ALL"
# #cmd ='top'
# stdin,stdout,stderr=ssh.exec_command(cmd,timeout=10)
# outlines=stdout.readlines()
# #print stdout.readlines() string[0].isdigit()
#
# for line in outlines:
#     if line[0].isdigit():
#       print(line)
#       print(line.split("   "))

import pexpect
