#!/usr/bin/env python
import rospy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState
from tf.transformations import quaternion_matrix, rotation_from_matrix

from giskardpy.tfwrapper import lookup_pose

def odom_cb(data):
    """
    :type data: Odometry
    :return:
    """
    global robot_pose
    js = JointState()
    js.header = data.header
    js.name = ['odom_x_joint', 'odom_y_joint', 'odom_z_joint']
    js.position = robot_pose
    js.velocity = [data.twist.twist.linear.x,
                   data.twist.twist.linear.y,
                   data.twist.twist.angular.z]
    js.effort = [0,0,0]
    js_pub.publish(js)

def robot_pose_cb(data):
    """
    :type data: PoseWithCovarianceStamped
    :return:
    """
    global robot_pose
    robot_pose = [data.pose.pose.position.x,
                  data.pose.pose.position.y,
                  rotation_from_matrix(quaternion_matrix([data.pose.pose.orientation.x,
                                                          data.pose.pose.orientation.y,
                                                          data.pose.pose.orientation.z,
                                                          data.pose.pose.orientation.w]))[0]]


rospy.init_node('base_joint_state_publisher')
robot_pose = [0,0,0]
js_pub = rospy.Publisher('base/joint_states', JointState, queue_size=10)
odom_sub = rospy.Subscriber('/base_odometry/odom', Odometry, odom_cb, queue_size=10)
robot_pose_sub = rospy.Subscriber('/robot_pose_ekf/odom_combined', PoseWithCovarianceStamped, robot_pose_cb, queue_size=10)
rospy.loginfo('{} running'.format(rospy.get_name()))
rospy.spin()