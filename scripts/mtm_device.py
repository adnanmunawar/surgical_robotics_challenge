import PyKDL
from PyKDL import Frame, Rotation, Vector
from geometry_msgs.msg import Pose, PoseStamped, TwistStamped, WrenchStamped, Wrench
from sensor_msgs.msg import Joy, JointState
import rospy
import time
import numpy as np


# Utilities
def kdl_frame_to_pose_msg(kdl_pose):
    ps = PoseStamped()
    p = ps.pose
    p.position.x = kdl_pose.p[0]
    p.position.y = kdl_pose.p[1]
    p.position.z = kdl_pose.p[2]

    p.orientation.x = kdl_pose.M.GetQuaternion()[0]
    p.orientation.y = kdl_pose.M.GetQuaternion()[1]
    p.orientation.z = kdl_pose.M.GetQuaternion()[2]
    p.orientation.w = kdl_pose.M.GetQuaternion()[3]

    return ps


def kdl_wrench_to_wrench_msg(kdl_wrench):
    ws = WrenchStamped()
    w = ws.wrench
    w.force.x = kdl_wrench.force[0]
    w.force.y = kdl_wrench.force[1]
    w.force.z = kdl_wrench.force[2]

    w.torque.x = kdl_wrench.torque[0]
    w.torque.y = kdl_wrench.torque[1]
    w.torque.z = kdl_wrench.torque[2]

    return ws


def pose_msg_to_kdl_frame(msg_pose):
    pose = msg_pose.pose
    f = Frame()
    f.p[0] = pose.position.x
    f.p[1] = pose.position.y
    f.p[2] = pose.position.z
    f.M = Rotation.Quaternion(pose.orientation.x,
                              pose.orientation.y,
                              pose.orientation.z,
                              pose.orientation.w)

    return f


# Init everything related to Geomagic
class MTM:
    # The name should include the full qualified prefix. I.e. '/Geomagic/', or '/omniR_' etc.
    def __init__(self, name):
        pose_sub_topic_name = name + 'position_cartesian_current'
        twist_topic_name = name + 'twist_body_current'
        gripper_topic_name = name + 'state_gripper_current'
        clutch_topic_name = '/dvrk/footpedals/clutch'
        coag_topic_name = '/dvrk/footpedals/coag'

        pose_pub_topic_name = name + 'set_position_cartesian'
        wrench_pub_topic_name = name + 'set_wrench_body'

        self._active = False
        self._scale = 1.0
        self.pose = Frame(Rotation().RPY(0, 0, 0), Vector(0, 0, 0))
        self.twist = PyKDL.Twist()
        R_off = Rotation.RPY(0, 0, 0)
        self._T_baseoffset = Frame(R_off, Vector(0, 0, 0))
        self._T_baseoffset_inverse = self._T_baseoffset.Inverse()
        self._T_tipoffset = Frame(Rotation().RPY(0, 0, 0), Vector(0, 0, 0))
        self.clutch_button_pressed = False # Used as Position Engage Clutch
        self.coag_button_pressed = False # Used as Position Engage Coag
        self.gripper_angle = 0

        self._pose_sub = rospy.Subscriber(pose_sub_topic_name, PoseStamped, self.pose_cb, queue_size=1)
        self._gripper_sub = rospy.Subscriber(gripper_topic_name, JointState, self.gripper_cb, queue_size=1)
        self._twist_sub = rospy.Subscriber(twist_topic_name, TwistStamped, self.twist_cb, queue_size=1)
        self._clutch_button_sub = rospy.Subscriber(clutch_topic_name, Joy, self.clutch_buttons_cb, queue_size=1)
        self._coag_button_sub = rospy.Subscriber(coag_topic_name, Joy, self.coag_buttons_cb, queue_size=1)

        self._pos_pub = rospy.Publisher(pose_pub_topic_name, Pose, queue_size=1)
        self._wrench_pub = rospy.Publisher(wrench_pub_topic_name, Wrench, queue_size=1)

        self.switch_slave = False

        self._button_msg_time = rospy.Time.now()
        self._switch_slave_duration = rospy.Duration(0.5)

        self.cur_pos_msg = None
        self.pre_coag_pose_msg = None

        print('Creating MTM Device Named: ', name, ' From ROS Topics')
        self._msg_counter = 0

    def set_base_frame(self, frame):
        self._T_baseoffset = frame
        self._T_baseoffset_inverse = self._T_baseoffset.Inverse()
        pass

    def set_tip_frame(self, frame):
        self._T_tipoffset = frame
        pass

    def set_scale(self, scale):
        self._scale = scale

    def get_scale(self):
        return self._scale

    def pose_cb(self, msg):
        self.cur_pos_msg = msg
        if self.pre_coag_pose_msg is None:
            self.pre_coag_pose_msg = self.cur_pos_msg
        cur_frame = pose_msg_to_kdl_frame(msg)
        cur_frame.p = cur_frame.p * self._scale
        self.pose = self._T_baseoffset_inverse * cur_frame * self._T_tipoffset
        # Mark active as soon as first message comes through
        self._active = True
        pass

    def is_active(self):
        return self._active

    def gripper_cb(self, msg):
        min = -1.8
        max = 1.4
        self.gripper_angle = msg.position[0] + min / (max - min)
        pass

    def twist_cb(self, msg):
        twist = PyKDL.Twist()
        omega = msg.twist
        twist[0] = omega.linear.x
        twist[1] = omega.linear.y
        twist[2] = omega.linear.z
        twist[3] = omega.angular.x
        twist[4] = omega.angular.y
        twist[5] = omega.angular.z
        self.twist = self._T_baseoffset_inverse * twist
        pass

    def clutch_buttons_cb(self, msg):
        self.clutch_button_pressed = msg.buttons[0]

        if self.clutch_button_pressed:
            time_diff = rospy.Time.now() - self._button_msg_time
            if time_diff.to_sec() < self._switch_slave_duration.to_sec():
                print('Allow Slave Switch')
                self.switch_slave = True
            self._button_msg_time = rospy.Time.now()

    def coag_buttons_cb(self, msg):
        self.coag_button_pressed = msg.buttons[0]
        self.pre_coag_pose_msg = self.cur_pos_msg
        if self.coag_button_pressed:
            time_diff = rospy.Time.now() - self._button_msg_time
            if time_diff.to_sec() < self._switch_slave_duration.to_sec():
                print('Allow Slave Switch')
                self.switch_slave = True
            self._button_msg_time = rospy.Time.now()

    def command_force(self, force):
        pass

    def move_cp(self, pose):
        if type(pose) == PyKDL.Frame:
            pose_msg = kdl_frame_to_pose_msg(pose).pose
        elif type(pose) == PoseStamped:
            pose_msg = pose.pose
        else:
            pose_msg = pose
        self._pos_pub.publish(pose_msg)

    def move_cf(self, wrench):
        wrench = self._T_baseoffset_inverse * wrench
        wrench_msg = kdl_wrench_to_wrench_msg(wrench)
        self._wrench_pub.publish(wrench_msg.wrench)

    def measured_cp(self):
        return self.pose

    def measured_cv(self):
        return self.twist

    def get_jaw_angle(self):
        return self.gripper_angle


def test():
    rospy.init_node('test_mtm')

    d = MTM('/dvrk/MTMR/')
    d.set_base_frame(Frame(Rotation.RPY(np.pi/2, 0, 0), Vector()))
    # rot_offset = Rotation.RPY(np.pi, np.pi/2, 0).Inverse()
    # tip_offset = Frame(rot_offset, Vector(0, 0, 0))
    # d.set_tip_frame(tip_offset)
    while not rospy.is_shutdown():
        if d.coag_button_pressed:
            t = PyKDL.Wrench()
            t.force = Vector(0, 0, 0)
            t.torque = Vector(0, 0, 0)
            d.move_cf(t)
        else:
            if d.is_active():
                d.move_cp(d.pre_coag_pose_msg)

        # [r, p, y] = d.measured_cp().M.GetRPY()
        # f = 180.0 / 3.1404
        # r = round(r * f, 2)
        # p = round(p * f, 2)
        # y = round(y * f, 2)
        # print('Roll: ', r, ', Pitch: ', p, ', Yaw: ', y)
        time.sleep(0.05)


if __name__ == '__main__':
    test()
