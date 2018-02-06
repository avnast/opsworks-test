#!/usr/bin/python

import boto3;
import sys;
import httplib;
import socket;
import re;
from datetime import datetime, timedelta;

# input params
monitored_hostnames = ('a.reals.org.ua', 'b.reals.org.ua', 'c.reals.org.ua');
max_image_age = timedelta(days=7);
check_timeout = 5;

image_tags = [
	{'Key': 'created_from_stopped', 'Value': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC%z')},
	{'Key': 'creator', 'Value': 'av.nast' }
];

# EC2 server resource
ec2 = boto3.resource('ec2');

# some logging
def LOG(message):
	sys.stderr.write(message+"\n");

# function to fetch instance name from tags
def get_instance_name_tag(instance):
	for tag in instance.tags:
		if tag['Key'] == 'Name':
			return tag['Value'];
	return '';

# delete image with snapshots
def delete_AMI(image):
	bdms = image.block_device_mappings;
	# store snapshots
	snapshot_ids = [];
	for bdm in bdms:
		try:
			snapshot_ids.append(bdm['Ebs']['SnapshotId']);
		except KeyError as e:
			LOG('WARNING: snapshot not found for AMI {} AKA "{}" device {}'.format(image.id, image.name, bdm['DeviceName']));
	# deregister AMI
	try:
		LOG('INFO:  deregistering image '+image.id);
		image.deregister();
	except Exception as e:
		LOG('ERROR DEREGISTERING IMAGE: '+str(e));
	# remove snapshots
	for snapshot_id in snapshot_ids:
		snapshot = ec2.Snapshot(snapshot_id);
		try:
			LOG('INFO:    deleting snapshot '+snapshot_id);
			snapshot.delete();
		except Exception as e:
			LOG('ERROR DELETING SNAPSHOT: '+str(e));

# create image from instance and set tags
def create_AMI(instance, image_tags):
	name = get_instance_name_tag(instance);
	now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC');
	LOG('INFO: creating image from stopped EC2 instance '+name);
	image = instance.create_image(
		Description = 'AMI automatically created from "'+name+'" '+now_str+' by av.nast because instance was stopped',
		Name = re.sub('[^\w\d\(\)\-\/\.]', '_', name+'/'+now_str)
	);
	image.create_tags(Tags = image_tags);
	# wait until created
	image.wait_until_exists();
	# and wait until data copied
	boto3.client('ec2').get_waiter('image_available').wait(ImageIds = [image.id]);

	return image;

# find and delete images older than max_age
def delete_old_images(max_age, image_tags, zone):
	tag_keys = [];
	for tag in image_tags:
		tag_keys.append(tag['Key']);

	if zone is None:
		images = ec2.images.filter(Filters=[
      {'Name': 'tag-key', 'Values': tag_keys }
		]);
	else:
		images = ec2.images.filter(Filters=[
			{'Name': 'tag-key', 'Values': tag_keys },
			{'Name': 'tag:AvailabilityZone', 'Values': [zone]}
		]);

	min_date = datetime.utcnow() - max_age;

	for image in images:
		try:
			image_creation_date = datetime.strptime(image.creation_date, '%Y-%m-%dT%H:%M:%S.000Z'); # 2018-02-05T18:43:27.000Z
		except Exception as e:
			LOG('WARNING: cannot parse creation date "'+image.creation_date+'" of '+image.name+': '+str(e));
			continue;
		
		if image_creation_date < min_date:
			LOG('INFO: image {} AKA "{}" is too old (creation_date = {})'.format(image.id, image.name, image.creation_date));
			delete_AMI(image);

# checks
def check_tcp_port(host, port):
	try:
		sock = socket.create_connection((host, port), check_timeout);
		sock.close();
		return 'OK';
	except Exception as e:
		LOG('WARNING: check_tcp_port({}, {}) failed: {}'.format(host, port, str(e)));
		return 'FAIL';

def check_http(hostname):
	conn = httplib.HTTPConnection(hostname, 80, 0, check_timeout);
	try:
		conn.request("HEAD", '/');
		res = conn.getresponse();
		if res.status == 200:
			return 'OK';
		else:
			LOG('WARNING: http_check(http://{}/) failed with status = {}'.format(hostname, res.status));
			return 'FAIL';
	except Exception as e:
		LOG('WARNING: http_check({}) failed: {}'.format(hostname, str(e)));
		return 'FAIL';

# get EC2 by our DNS record
def get_ec2_instance_by_hostname(hostname):
	ip = socket.gethostbyname(hostname);
	ec2_res = ec2.instances.filter(Filters=[{'Name': 'ip-address', 'Values': [ip]}]);
	for instance in ec2_res:
		return instance;
	return None;

# fine grained output with highlighting :)
def hl(match_obj):
	spaces = match_obj.group(1);
	message = match_obj.group(2);
	norm = "\033[0;37;40m";
	ok = "\033[0;32;40m";
	fail = "\033[0;31;40m";
	if message.lower() == 'ok' or message.lower() == 'running':
		return spaces+ok+message+norm;
	else:
		return spaces+fail+message+norm;

def fine_grained_output(status_array):
	fmt = '{:<20} {:^10} {:^10} {:^10}';
	hline = ''.ljust(52, '-');
	# header
	print hline;
	print str.format(fmt, 'hostname', 'TCP', 'HTTP', 'EC2 state');
	print hline;
	# rows
	for hostname, data in status_array.items():
		row = str.format(fmt, hostname, data['tcp'], data['http'], data['ec2_state']);
		row = re.sub(r'(\s+)\b(\w+)\b', hl, row); # coloring
		print row;
	print hline;

# fill status data with actual values
def update_status(data):
	if data is None:
		data = {};
	for hostname in monitored_hostnames:
		if hostname in data:
			if data[hostname]['ec2_instance'] is None:
				instance = None;
			else:
				instance = ec2.Instance(data[hostname]['ec2_instance']);
		else:
			instance = get_ec2_instance_by_hostname(hostname);
		if instance is None:
			LOG('WARNING: cannot get EC2 instance '+hostname);
			instance_id = None;
			instance_state = 'unknown';
		else:
			instance_id = instance.id;
			instance_state = instance.state['Name'];
		data[hostname] = {
			'tcp': check_tcp_port(hostname, 22), # TCP check? let it be check for open tcp port
			'http': check_http(hostname),
			'ec2_instance': instance_id,
			'ec2_state': instance_state
		};
  
########################### MAIN ######################################

# prepare get status info before script actions
status = {};
update_status(status);

print "\nStatus BEFORE we act:";
fine_grained_output(status);

# action
for hostname, data in status.items():
	if data['ec2_instance'] is None:
		continue;
	if data['ec2_state'] == 'stopped':
		instance = ec2.Instance(data['ec2_instance']);
		LOG('OOPS: instance {} AKA "{}" stopped'.format(instance.id, get_instance_name_tag(instance)));
		# images have no relations with availability zones, so store it to tag
		zone = instance.placement['AvailabilityZone'];
		image_tags.append({'Key': 'AvailabilityZone', 'Value': zone});

		try:
			image = create_AMI(instance, image_tags);
			# terminate stopped instance
			LOG('INFO: terminating EC2 {} AKA "{}"'.format(instance.id, get_instance_name_tag(instance)));
			try:
				instance.terminate();
			except Exception as e:
				LOG('ERROR TERMINATING INSTANCE: '+str(e));
		except Exception as e:
			LOG('ERROR CREATING IMAGE: '+str(e));

		# check for obsolete images
		delete_old_images(max_image_age, image_tags, zone);

update_status(status);

print "\nStatus AFTER we act:";
fine_grained_output(status);

