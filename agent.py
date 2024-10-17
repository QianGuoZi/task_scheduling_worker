import atexit
import json
import os
import socket
import subprocess as sp
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED
from typing import Dict

import requests
from flask import Flask, request

class classInfo:
    def __init__(self,node_name: str, node: str):
        self.formNode = node_name
        self.toNode = node

# DO NOT change this port number.
agent_port = 3333
executor = ThreadPoolExecutor ()
lock = threading.RLock ()
app = Flask (__name__)
dirname = os.path.abspath (os.path.dirname (__file__))
hostname = socket.gethostname ()
heartbeat = {}
tc_data = {}
physical_nic = ''
ctl_addr = ''
dml_p: sp.Popen
classid : Dict[classInfo,int]={}
classNum : Dict[str,int]={}
linkDict : Dict[str,set]={}



@app.route ('/hi', methods=['GET'])
def route_hi ():
	# 返回主机名称
	return 'this is agent ' + hostname + '\n'


@app.route ('/heartbeat', methods=['GET'])
def route_heartbeat ():
	# 收集心跳，假如是第一次从仿真节点接收，则需要部署容器的tc设置，用于模拟网络
	"""
	listen message from worker/worker_utils.py, heartbeat ().
	it will store the time of nodes heartbeat.
	when it receives the heartbeat of an emulated node for the first time,
	it will deploy the container's tc settings.
	"""
	name = request.args.get ('name')
	t_time = time.time ()
	with lock:
		# deploy the emulated node's tc settings.
		if name not in heartbeat and name in tc_data:
			ret = {}
			deploy_emulated_tc (name, ret)
			# this request can be received by controller/base/node.py, route_emulated_tc ().
			requests.post ('http://' + ctl_addr + '/emulated/tc', data={'data': json.dumps (ret)})
		heartbeat [name] = t_time
	return ''


def deploy_emulated_tc(name: str, ret: Dict):
    # 部署仿真节点的tc设置
    node_name = name
    time_start = time.time()
    data = tc_data[name]
    prefix = 'sudo docker exec ' + name + ' '
    # 清理旧的tc设置
    clear_old_tc(prefix, data['NET_NODE_NIC'])
    # 配置新的tc设置
    msg = create_new_tc(prefix, data['NET_NODE_NIC'], data['NET_NODE_TC'],
                        data['NET_NODE_TC_IP'], data['NET_NODE_TC_PORT'], node_name)
    if msg == '':
        print(name + ' tc succeed')
        with lock:
            ret[name] = {'number': len(data['NET_NODE_TC'])}
    else:
        print(name + ' tc failed, err:')
        print(msg)
        with lock:
            ret[name] = {'msg': msg}
    time_end = time.time()
    print('all time cost', time_end - time_start, 's')

def clear_old_tc(prefix: str, nic: str):
    # 清除旧的tc设置
    # time_start = time.time()
    cmd = prefix + ' tc qdisc show dev %s' % nic
    p = sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.STDOUT, shell=True)
    msg = p.communicate()[0].decode()
    if "priomap" not in msg and "noqueue" not in msg:
        cmd = prefix + ' tc qdisc del dev %s root' % nic
        sp.Popen(cmd, stdout=sp.PIPE, stderr=sp.STDOUT, shell=True).wait()
    # time_end = time.time()
    # print('delete time cost', time_end - time_start, 's')


def create_new_tc(prefix: str, nic: str, tc: Dict[str, str], tc_ip: Dict[str, str],
                  tc_port: Dict[str, int], node_name: str):
    global classNum
    global classid
    # 配置新的tc设置
    if not tc:
        return ''

    cmd = ['%s tc qdisc add dev %s root handle 1: htb default 1' % (prefix, nic),
           '%s tc class add dev %s parent 1: classid 1:1 htb rate 10gbps ceil 10gbps burst 15k' % (prefix, nic)]
    num = classNum.get(node_name,10)
    nodeset = set()
    if linkDict.get(node_name) != None:
        nodeset = linkDict[node_name]
    for name in tc.keys():
        bw = tc[name]
        ip = tc_ip[name]
        port = tc_port[name]
        cmd.append('%s tc class add dev %s parent 1:1 classid ' % (prefix, nic)
                   + '1:%d htb rate %s ceil %s burst 15k' % (num, bw, bw))
        cmd.append('%s tc filter add dev %s protocol ip parent 1: prio 2 u32 match ip dst ' % (prefix, nic)
                   + '%s/32 match ip dport %d 0xffff flowid 1:%d' % (ip, port, num))
        nodepair = classInfo(node_name,name)
        classid[nodepair] = num
        print(node_name + ' to ' + name + ' link id is: ' + str(classid[nodepair]))
        nodeset.add(name)
        num += 1
    classNum[node_name] = num
    linkDict[node_name] = nodeset
    print(node_name + 'classNum: ' + str(classNum[node_name]))
    for name in linkDict[node_name]:
        print(node_name + 'has: ' + name)
    p = sp.Popen(' && '.join(cmd), stdout=sp.PIPE, stderr=sp.STDOUT, shell=True, close_fds=True)
    msg = p.communicate()[0].decode()
    return msg

@app.route ('/heartbeat/all', methods=['GET'])
def route_heartbeat_all ():
	# 通过一个GET请求来查看发送一个heartbeat的时间开销
	"""
	you can send a GET request to this /heartbeat/all to get
	how much time has passed since nodes last sent a heartbeat.
	"""
	s = 'the last heartbeat of nodes are:\n'
	now = time.time ()
	for name in heartbeat:
		_time = now - heartbeat [name]
		s = s + name + ' was ' + str (_time) + ' seconds ago. ' \
		    + 'it should be less than 30s.\n'
	return s


@app.route ('/heartbeat/abnormal', methods=['GET'])
def route_abnormal_heartbeat ():
	# 通过发送GET请求获取可能异常的节点
	"""
	you can send a GET request to this /heartbeat/abnormal to get
	the likely abnormal nodes.
	"""
	s = 'the last heartbeat of likely abnormal nodes are:\n'
	now = time.time ()
	for name in heartbeat:
		_time = now - heartbeat [name]
		if _time > 30:
			s = s + name + ' was ' + str (_time) + ' seconds ago. ' \
			    + 'it should be less than 30s.\n'
	return s


@app.route ('/emulator/info', methods=['GET'])
def route_emulator_info ():
	# 从controller层获取controller的ip、port和模拟器的名称
	"""
	listen message from controller/base/node.py, send_emulator_info ().
	save the ${ip:port} of ctl and emulator's name.
	"""
	global ctl_addr, hostname
	ctl_addr = request.args.get ('address')
	hostname = request.args.get ('name')
	return ''

# def route_emulated_ovs ()：服务器接收controller的网络配置信息，然后进行组网

@app.route ('/emulated/tc', methods=['POST'])
def route_emulated_tc ():
	# 从controller层获取tc设置的内容
	"""
	listen message from controller/base/node.py, send_emulated_tc ().
	after emulated nodes are ready, it will deploy emulated nodes' tc settings.
	"""
	data = json.loads (request.form ['data'])
	print (data)
	tc_data.update (data)
	return ''


@app.route ('/emulated/tc/update', methods=['POST'])
def route_emulated_tc_update ():
	# 从controller层获取tc设置的更新
	"""
	listen message from controller/base/manager.py, update_emulated_tc ().
	after emulated nodes are ready, it will deploy emulated nodes' tc settings.
	"""
	data = json.loads (request.form ['data'])
	print (data)
	tc_data.update (data)

	ret = {}
	tasks = []
	for name in data:
		tasks.append (executor.submit (deploy_emulated_tc, name, ret))
	wait (tasks, return_when=ALL_COMPLETED)
	return json.dumps (ret)


@app.route('/emulated/node/update', methods=['POST'])
def route_emulated_node_update():
	"""
	从manager获取更新的内容，修改镜像的cpu、ram和ram_swap
	"""
	time_start = time.time()
	cpus = request.form['cpus']
	rams = request.form['rams']
	ram_swap = request.form['ram_swap']
	node_name = request.form['node_name']
	cmd = 'sudo docker update --cpus ' + cpus + ' --memory ' + rams + 'M --memory-swap ' + ram_swap + 'M ' + node_name
	print(cmd)
	p = sp.Popen(cmd, shell=True, stdout=sp.PIPE, stderr=sp.STDOUT)
	msg = p.communicate()[0].decode()  # TODO:bug
	print('msg : ' + msg)
	time_end = time.time()
	print('emulated node update time cost', time_end - time_start, 's')
	if node_name in msg:
		print('update node succeed')
		ret = {'msg': 'update node succeed'}
		return json.dumps(ret)
	else:
		print('update node failed')
		ret = {'msg': 'update node failed'}
		return json.dumps(ret)


@app.route('/emulated/node/stop', methods=['GET'])
def route_emulated_node_stop():
    # 从controller层获取暂停指令，heartbeat会清空，用docker-compose暂停某个容器
    time_start = time.time()
    node_name = request.args.get('node_name')
    print(node_name)
    cmd = 'sudo docker-compose -f ' + hostname + '.yml stop ' + node_name
    print(cmd)
    sp.Popen(cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait()
    time_end = time.time()
    print('stop time cost', time_end - time_start, 's')
    return ''
    

@app.route ('/emulated/build', methods=['POST'])
def route_emulated_build ():
	# 从controller层获取模拟器docker相关的信息ym文件，创建image
	"""
	listen file from controller/base/node.py, build_emulated_env ().
	it will use these files to build a docker image.
	"""
	path = os.path.join (dirname, 'Dockerfile')
	request.files.get ('Dockerfile').save (path)
	request.files.get ('dml_req').save (os.path.join (dirname, 'dml_req.txt'))
	tag = request.form ['tag']
	cmd = 'sudo docker build -t ' + tag + ' -f ' + path + ' .'
	print (cmd)
	p = sp.Popen (cmd, shell=True, stdout=sp.PIPE, stderr=sp.STDOUT)
	msg = p.communicate () [0].decode ()
	print (msg)
	if 'Successfully tagged' in msg:
		print ('build image succeed')
		return '1'
	else:
		print ('build image failed')
		print ('build image failed')
		return '-1'


@app.route ('/emulated/launch', methods=['POST'])
def route_emulated_launch ():
	# 从controller层获取yml文件，heartbeat会清空，用docker-compose开启容器
	"""
	listen file from controller/base/node.py, launch_emulated ().
	it will launch the yml file.
	"""
	heartbeat.clear ()
	filename = os.path.join (dirname, hostname + '.yml')
	request.files.get ('yml').save (filename)
	cmd = 'sudo docker-compose -f ' + filename + ' up'
	print (cmd)
	sp.Popen (cmd, shell=True, stderr=sp.STDOUT)
	return ''


@app.route ('/emulated/stop', methods=['GET'])
def route_emulated_stop ():
	# 从controller层获取暂停指令，heartbeat会清空，用docker-compose暂停容器
	"""
	listen message from controller/base/manager.py, stop_emulated ().
	it will stop the above yml file.
	"""
	cmd = 'sudo docker-compose -f ' + hostname + '.yml stop'
	print (cmd)
	sp.Popen (cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait ()
	heartbeat.clear ()
	return ''


@app.route('/emulated/node/remove', methods=['GET'])
def route_emulated_node_remove():
    time_start = time.time()
    # 从controller层获取移除指令，heartbeat会清空，用docker-compose移除某个容器
    node_name = request.args.get('node_name')
    cmd = 'sudo docker-compose -f ' + hostname + '.yml stop ' + node_name + ' && sudo docker-compose -f ' \
          + hostname + '.yml rm -f ' + node_name
    print(cmd)
    sp.Popen(cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait()
    time_end = time.time()
    print('docker remove time cost', time_end - time_start, 's')
    return ''


@app.route ('/emulated/clear', methods=['GET'])
def route_emulated_clear ():
	# 从controller层获取终止指令，heartbeat会清空，用docker-compose终止容器，删除yml文件
	"""
	listen message from controller/base/manager.py, clear_emulated ().
	it will clear the above yml file.
	"""
	cmd = 'sudo docker-compose -f ' + hostname + '.yml down -v'
	print (cmd)
	sp.Popen (cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait ()
	heartbeat.clear ()
	return ''


@app.route ('/emulated/reset', methods=['GET'])
def route_emulated_reset ():
	# 删除所有docker容器、网络和数据卷
	"""
	listen message from controller/base/manager.py, reset_emulated ().
	it will remove all docker containers, networks and volumes.
	"""
	cmd = ['sudo docker rm -f $(docker ps -aq)',
	       'sudo docker network rm $(docker network ls -q)',
	       'sudo docker volume rm $(docker volume ls -q)']
	for c in cmd:
		print (c)
		sp.Popen (c, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait ()
	heartbeat.clear ()
	return ''


@app.route ('/physical/nfs', methods=['POST'])
def route_physical_nfs ():
	# 从controller层获取NFS设置信息，挂载NFS路径
	"""
	listen message from controller/base/node.py, send_physical_nfs ().
	it will mount the nfs path.
	"""
	route_physical_clear_nfs ()
	mounted = ''
	data = json.loads (request.form ['data'])
	print (data)
	ip = data ['ip']
	nfs = data ['nfs']
	err = []
	for nfs_path in nfs:
		local_path = nfs [nfs_path]
		# is relative path.
		if local_path [0] != '/':
			local_path = os.path.abspath (os.path.join (dirname, local_path))
		if not os.path.exists (local_path):
			os.makedirs (local_path)
		cmd = 'sudo mount -t nfs ' + ip + ':' + nfs_path + ' ' + local_path
		print (cmd)
		p = sp.Popen (cmd, shell=True, stdout=sp.PIPE, stderr=sp.STDOUT)
		msg = p.communicate () [0].decode ()
		if msg == '':
			print ('mount nfs succeed')
			mounted += local_path + '\n'
		else:
			print ('mount nfs failed, err:')
			print (msg)
			err.append (msg)

	# record the path of mounted.
	with open (os.path.join (dirname, 'mounted.txt'), 'w') as f:
		f.write (mounted)
	return json.dumps (err)


@app.route ('/physical/tc', methods=['POST'])
def route_physical_tc ():
	# 从controller层获取tc配置信息，清空旧的设置，然后创建新的
	"""
	listen message from controller/base/node.py, send_physical_tc ()
	and controller/base/manager.py, update_physical_tc ().
	it will clear the old tc settings and apply the new one.
	"""
	data = json.loads (request.form ['data'])
	prefix = 'sudo '
	clear_old_tc (prefix, data ['NET_NODE_NIC'])
	msg = create_new_tc (prefix, data ['NET_NODE_NIC'], data ['NET_NODE_TC'],
		data ['NET_NODE_TC_IP'], data ['NET_NODE_TC_PORT'])
	if msg == '':
		print ('tc succeed')
	else:
		print ('tc failed, err:')
		print (msg)
	return msg


@app.route ('/physical/variable', methods=['POST'])
def route_physical_variable ():
	# 从controller层获取物理变量信息
	"""
	listen message from controller/base/node.py, send_physical_variable ().
	it will save the variables.
	"""
	global ctl_addr, physical_nic
	atexit.register (route_physical_reset)
	data = json.loads (request.form ['data'])
	ctl_addr = data ['NET_CTL_ADDRESS']
	physical_nic = data ['NET_NODE_NIC']
	print (data)
	for k, v in data.items ():
		# os.putenv (k, v) # has no effect.
		os.environ [k] = v
	return ''


@app.route ('/physical/build', methods=['POST'])
def route_physical_build ():
	# 从controller层获取dml_req.txt文件，通过pip安装相关的包
	"""
	listen file from controller/base/node.py, build_physical_env ().
	it will install the dml_req.txt by pip.
	"""
	path = os.path.join (dirname, 'dml_req.txt')
	request.files.get ('dml_req').save (path)
	# double check it because you should probably run
	# [pip install] or [sudo pip install] or [pip3 install] instead of this default one.
	cmd = 'sudo pip3 install -r ' + path
	print (cmd)
	sp.Popen (cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait ()
	# if the above installation is successful, it will output {package==version}.
	cmd = 'sudo pip3 freeze -r ' + path
	print (cmd)
	p = sp.Popen (cmd, shell=True, stdout=sp.PIPE, stderr=sp.STDOUT)
	msg = p.communicate () [0].decode ()
	# just need the packages in dml_req.txt.
	msg = msg [:msg.find ('#')].upper ()
	req = []
	err = []
	with open (path, 'r') as f:
		for line in f:
			if line.rfind ('=') != -1:
				line = line [:line.rfind ('=') - 1]
			req.append (line.replace ('\r', '').replace ('\n', '').upper ())
	for r in req:
		if r not in msg:
			err.append (r)
	if err:
		print ('req failed, err:')
		print (err)
		return '-1'
	else:
		print ('req succeed')
		return '1'


@app.route ('/physical/launch', methods=['POST'])
def route_physical_launch ():
	# 从controller层获取信息，创建一个新进程去执行working_dir下的指令cmd
	"""
	listen message from controller/base/node.py, launch_physical ().
	it will launch a new process to execute the ${cmd} at ${working_dir}.
	"""
	global dml_p
	data = json.loads (request.form ['data'])
	working_dir = data ['dir']
	# is relative path.
	if working_dir [0] != '/':
		working_dir = os.path.abspath (os.path.join (dirname, working_dir))
	cmd = ' '.join (data ['cmd'])
	print ('CWD ' + working_dir + ' RUN ' + cmd)
	dml_p = sp.Popen (cmd, cwd=working_dir, shell=True, stderr=sp.STDOUT)
	return ''


@app.route ('/physical/stop', methods=['GET'])
def route_physical_stop ():
	# 杀死上一个函数创建的进程
	"""
	listen message from controller/base/manager.py, stop_physical ().
	it will kill the process started by above route_physical_launch ().
	"""
	try:
		if dml_p.poll () is None:
			# ${dml_p.pid} is the pid of the shell process,
			# because we use shell to execute the ${cmd}.
			# in most cases, the pid of ${cmd} is ${dml_p.pid}+1,
			# so we just try to terminate ${dml_p.pid}+1.
			# hopefully we don't terminate other processes by mistake :-)
			os.kill (dml_p.pid + 1, 3)
	except  NameError:
		pass
	finally:
		return ''


@app.route ('/physical/clear/tc', methods=['GET'])
def route_physical_clear_tc ():
	# 重置tc设置
	"""
	listen message from controller/base/manager.py, clear_physical_tc ().
	it will reset tc settings.
	"""
	clear_old_tc ('sudo ', physical_nic)
	return ''


@app.route ('/physical/clear/nfs', methods=['GET'])
def route_physical_clear_nfs ():
	# 重置NFS设置
	"""
	listen message from controller/base/manager.py, clear_physical_nfs ().
	it will reset nfs.
	"""
	path = os.path.join (dirname, 'mounted.txt')
	if os.path.exists (path):
		with open (path, 'r') as f:
			for line in f:
				cmd = 'sudo umount -t nfs -l ' + line
				print (cmd)
				sp.Popen (cmd, shell=True, stdout=sp.DEVNULL, stderr=sp.STDOUT).wait ()
	return ''


@app.route ('/physical/reset', methods=['GET'])
def route_physical_reset ():
	# 先杀死创建的进程， 然后重置tc和NFS设置
	"""
	listen message from controller/base/manager.py, reset_physical ().
	it will kill the process started by above route_physical_start (),
	reset tc settings and reset nfs.
	"""
	route_physical_stop ()
	clear_old_tc ('sudo ', physical_nic)
	route_physical_clear_nfs ()
	return ''


app.run (host='0.0.0.0', port=agent_port, threaded=True)
