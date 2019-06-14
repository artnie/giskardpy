import numpy as np
from collections import OrderedDict

from geometry_msgs.msg import Vector3Stamped, Vector3
from rospy_message_converter.message_converter import convert_dictionary_to_ros_message
from scipy.optimize import curve_fit

import giskardpy.identifier as identifier
import symengine_wrappers as sw
from giskardpy.input_system import PoseStampedInput, Point3Input, Vector3Input, Vector3StampedInput, FrameInput
from giskardpy.qp_problem_builder import SoftConstraint
from giskardpy.tfwrapper import transform_pose, transform_vector

MAX_WEIGHT = 15
HIGH_WEIGHT = 5
MID_WEIGHT = 1
LOW_WEIGHT = 0.5
ZERO_WEIGHT = 0


class Constraint(object):
    def __init__(self, god_map, **kwargs):
        self.god_map = god_map

    def save_params_on_god_map(self, params):
        constraints = self.get_god_map().safe_get_data(identifier.constraints_identifier)
        constraints[str(self)] = params
        self.get_god_map().safe_set_data(identifier.constraints_identifier, constraints)

    def get_constraint(self, **kwargs):
        pass

    def get_identifier(self):
        return identifier.constraints_identifier + [str(self)]

    def get_world_object_pose(self, object_name, link_name):
        pass

    def get_god_map(self):
        """
        :rtype: giskardpy.god_map.GodMap
        """
        return self.god_map

    def get_world(self):
        """
        :rtype: giskardpy.world.World
        """
        return self.get_god_map().safe_get_data(identifier.world)

    def get_robot(self):
        """
        :rtype: giskardpy.symengine_robot.Robot
        """
        return self.get_god_map().safe_get_data(identifier.robot)

    def get_world_unsafe(self):
        """
        :rtype: giskardpy.world.World
        """
        return self.get_god_map().get_data(identifier.world)

    def get_robot_unsafe(self):
        """
        :rtype: giskardpy.symengine_robot.Robot
        """
        return self.get_god_map().get_data(identifier.robot)

    def get_input_joint_position(self, joint_name):
        key = identifier.joint_states + [joint_name, u'position']
        return self.god_map.to_symbol(key)

    def __str__(self):
        return self.__class__.__name__

    def get_fk(self, root, tip):
        return self.get_robot().get_fk_expression(root, tip)

    def get_fk_evaluated(self, root, tip):
        return FrameInput(self.get_god_map().to_symbol,
                          prefix=identifier.fk_np +
                                 [(root, tip)]).get_frame()

    def get_input_float(self, name):
        key = self.get_identifier() + [name]
        return self.god_map.to_symbol(key)

    def get_input_PoseStamped(self, name):
        return PoseStampedInput(self.get_god_map().to_symbol,
                                translation_prefix=self.get_identifier() +
                                                   [name,
                                                    u'pose',
                                                    u'position'],
                                rotation_prefix=self.get_identifier() +
                                                [name,
                                                 u'pose',
                                                 u'orientation']).get_frame()

    def get_input_Vector3Stamped(self, name):
        return Vector3StampedInput(self.god_map.to_symbol,
                                   vector_prefix=self.get_identifier() + [name, u'vector']).get_expression()


class JointPosition(Constraint):
    goal = u'goal'
    weight = u'weight'
    gain = u'gain'
    max_speed = u'max_speed'

    def __init__(self, god_map, joint_name, goal, weight=LOW_WEIGHT, gain=10, max_speed=1):
        super(JointPosition, self).__init__(god_map)
        self.joint_name = joint_name
        params = {self.goal: goal,
                  self.weight: weight,
                  self.gain: gain,
                  self.max_speed: max_speed}
        self.save_params_on_god_map(params)

    def get_constraint(self):
        """
        example:
        name='JointPosition'
        parameter_value_pair='{
            "joint_name": "torso_lift_joint", #required
            "goal_position": 0, #required
            "weight": 1, #optional
            "gain": 10, #optional -- error is multiplied with this value
            "max_speed": 1 #optional -- rad/s or m/s depending on joint; can not go higher than urdf limit
        }'
        :return:
        """

        current_joint = self.get_input_joint_position(self.joint_name)

        joint_goal = self.get_input_float(self.goal)
        weight = self.get_input_float(self.weight)
        p_gain = self.get_input_float(self.gain)
        max_speed = self.get_input_float(self.max_speed)

        soft_constraints = OrderedDict()

        if self.get_robot().is_joint_continuous(self.joint_name):
            err = sw.shortest_angular_distance(current_joint, joint_goal)
        else:
            err = joint_goal - current_joint
        capped_err = sw.diffable_max_fast(sw.diffable_min_fast(p_gain * err, max_speed), -max_speed)

        soft_constraints[str(self)] = SoftConstraint(lower=capped_err,
                                                     upper=capped_err,
                                                     weight=weight,
                                                     expression=current_joint)
        # add_debug_constraint(soft_constraints, str(self)+'/dist', err)
        return soft_constraints

    def __str__(self):
        return u'{}/{}'.format(self.__class__.__name__, self.joint_name)


class JointPositionList(Constraint):
    def __init__(self, god_map, goal_state, weight=LOW_WEIGHT, gain=10, max_speed=1):
        super(JointPositionList, self).__init__(god_map)
        self.constraints = []
        for i, joint_name in enumerate(goal_state[u'name']):
            goal_position = goal_state[u'position'][i]
            self.constraints.append(JointPosition(god_map, joint_name, goal_position, weight, gain, max_speed))

    def get_constraint(self):
        soft_constraints = OrderedDict()
        for constraint in self.constraints:
            soft_constraints.update(constraint.get_constraint())
        return soft_constraints


class CartesianConstraint(Constraint):
    goal = u'goal'
    weight = u'weight'
    gain = u'gain'
    max_speed = u'max_speed'

    def __init__(self, god_map, root_link, tip_link, goal, weight=MID_WEIGHT, gain=3, max_speed=0.1):
        super(CartesianConstraint, self).__init__(god_map)
        self.root = root_link
        self.tip = tip_link
        goal = convert_dictionary_to_ros_message(u'geometry_msgs/PoseStamped', goal)
        goal = transform_pose(self.root, goal)

        # make sure rotation is normalized quaternion
        # TODO make a function out of this
        rotation = np.array([goal.pose.orientation.x,
                             goal.pose.orientation.y,
                             goal.pose.orientation.z,
                             goal.pose.orientation.w])
        normalized_rotation = rotation / np.linalg.norm(rotation)
        goal.pose.orientation.x = normalized_rotation[0]
        goal.pose.orientation.y = normalized_rotation[1]
        goal.pose.orientation.z = normalized_rotation[2]
        goal.pose.orientation.w = normalized_rotation[3]

        params = {self.goal: goal,
                  self.weight: weight,
                  self.gain: gain,
                  self.max_speed: max_speed}
        self.save_params_on_god_map(params)

    def get_goal_pose(self):
        return self.get_input_PoseStamped(self.goal)

    def __str__(self):
        return u'{}/{}/{}'.format(self.__class__.__name__, self.root, self.tip)


class CartesianPosition(CartesianConstraint):

    def get_constraint(self):
        """
        example:
        name='CartesianPosition'
        parameter_value_pair='{
            "root": "base_footprint", #required
            "tip": "r_gripper_tool_frame", #required
            "goal_position": {"header":
                                {"stamp":
                                    {"secs": 0,
                                    "nsecs": 0},
                                "frame_id": "",
                                "seq": 0},
                            "pose": {"position":
                                        {"y": 0.0,
                                        "x": 0.0,
                                        "z": 0.0},
                                    "orientation": {"y": 0.0,
                                                    "x": 0.0,
                                                    "z": 0.0,
                                                    "w": 0.0}
                                    }
                            }', #required
            "weight": 1, #optional
            "gain": 3, #optional -- error is multiplied with this value
            "max_speed": 0.3 #optional -- rad/s or m/s depending on joint; can not go higher than urdf limit
        }'
        :return:
        """

        goal_position = sw.position_of(self.get_goal_pose())
        weight = self.get_input_float(self.weight)
        gain = self.get_input_float(self.gain)
        max_speed = self.get_input_float(self.max_speed)

        current_position = sw.position_of(self.get_fk(self.root, self.tip))

        soft_constraints = OrderedDict()

        trans_error_vector = goal_position - current_position
        trans_error = sw.norm(trans_error_vector)
        trans_scale = sw.diffable_min_fast(trans_error * gain, max_speed)
        trans_control = trans_error_vector / trans_error * trans_scale

        soft_constraints[str(self) + u'x'] = SoftConstraint(lower=trans_control[0],
                                                            upper=trans_control[0],
                                                            weight=weight,
                                                            expression=current_position[0])
        soft_constraints[str(self) + u'y'] = SoftConstraint(lower=trans_control[1],
                                                            upper=trans_control[1],
                                                            weight=weight,
                                                            expression=current_position[1])
        soft_constraints[str(self) + u'z'] = SoftConstraint(lower=trans_control[2],
                                                            upper=trans_control[2],
                                                            weight=weight,
                                                            expression=current_position[2])
        return soft_constraints


class CartesianOrientation(CartesianConstraint):
    def __init__(self, god_map, root_link, tip_link, goal, weight=HIGH_WEIGHT, gain=3, max_speed=0.5):
        super(CartesianOrientation, self).__init__(god_map, root_link, tip_link, goal, weight, gain, max_speed)

    def get_constraint(self):
        """
        example:
        name='CartesianPosition'
        parameter_value_pair='{
            "root": "base_footprint", #required
            "tip": "r_gripper_tool_frame", #required
            "goal_position": {"header":
                                {"stamp":
                                    {"secs": 0,
                                    "nsecs": 0},
                                "frame_id": "",
                                "seq": 0},
                            "pose": {"position":
                                        {"y": 0.0,
                                        "x": 0.0,
                                        "z": 0.0},
                                    "orientation": {"y": 0.0,
                                                    "x": 0.0,
                                                    "z": 0.0,
                                                    "w": 0.0}
                                    }
                            }', #required
            "weight": 1, #optional
            "gain": 3, #optional -- error is multiplied with this value
            "max_speed": 0.3 #optional -- rad/s or m/s depending on joint; can not go higher than urdf limit
        }'
        :return:
        """
        goal_rotation = sw.rotation_of(self.get_goal_pose())
        weight = self.get_input_float(self.weight)
        gain = self.get_input_float(self.gain)
        max_speed = self.get_input_float(self.max_speed)

        current_rotation = sw.rotation_of(self.get_fk(self.root, self.tip))
        current_evaluated_rotation = sw.rotation_of(self.get_fk_evaluated(self.root, self.tip))

        soft_constraints = OrderedDict()
        axis, angle = sw.diffable_axis_angle_from_matrix((current_rotation.T * goal_rotation))

        capped_angle = sw.diffable_max_fast(sw.diffable_min_fast(gain * angle, max_speed), -max_speed)

        r_rot_control = axis * capped_angle

        hack = sw.rotation_matrix_from_axis_angle([0, 0, 1], 0.0001)

        axis, angle = sw.diffable_axis_angle_from_matrix((current_rotation.T * (current_evaluated_rotation * hack)).T)
        c_aa = (axis * angle)

        soft_constraints[str(self) + u'/0'] = SoftConstraint(lower=r_rot_control[0],
                                                             upper=r_rot_control[0],
                                                             weight=weight,
                                                             expression=c_aa[0])
        soft_constraints[str(self) + u'/1'] = SoftConstraint(lower=r_rot_control[1],
                                                             upper=r_rot_control[1],
                                                             weight=weight,
                                                             expression=c_aa[1])
        soft_constraints[str(self) + u'/2'] = SoftConstraint(lower=r_rot_control[2],
                                                             upper=r_rot_control[2],
                                                             weight=weight,
                                                             expression=c_aa[2])
        return soft_constraints


class CartesianOrientationSlerp(CartesianConstraint):
    def __init__(self, god_map, root_link, tip_link, goal, weight=MID_WEIGHT, gain=6, max_speed=0.5):
        super(CartesianOrientationSlerp, self).__init__(god_map, root_link, tip_link, goal, weight, gain, max_speed)

    def get_constraint(self):
        """
        example:
        name='CartesianPosition'
        parameter_value_pair='{
            "root": "base_footprint", #required
            "tip": "r_gripper_tool_frame", #required
            "goal_position": {"header":
                                {"stamp":
                                    {"secs": 0,
                                    "nsecs": 0},
                                "frame_id": "",
                                "seq": 0},
                            "pose": {"position":
                                        {"y": 0.0,
                                        "x": 0.0,
                                        "z": 0.0},
                                    "orientation": {"y": 0.0,
                                                    "x": 0.0,
                                                    "z": 0.0,
                                                    "w": 0.0}
                                    }
                            }', #required
            "weight": 1, #optional
            "gain": 3, #optional -- error is multiplied with this value
            "max_speed": 0.3 #optional -- rad/s or m/s depending on joint; can not go higher than urdf limit
        }'
        :return:
        """
        goal_rotation = sw.rotation_of(self.get_goal_pose())
        weight = self.get_input_float(self.weight)
        gain = self.get_input_float(self.gain)
        max_speed = self.get_input_float(self.max_speed)

        current_rotation = sw.rotation_of(self.get_fk(self.root, self.tip))
        current_evaluated_rotation = sw.rotation_of(self.get_fk_evaluated(self.root, self.tip))

        soft_constraints = OrderedDict()

        axis, angle = sw.diffable_axis_angle_from_matrix((current_rotation.T * goal_rotation))
        angle = sw.diffable_abs(angle)

        capped_angle = sw.diffable_min_fast(max_speed / (gain * angle), 1)

        q1 = sw.quaternion_from_matrix(current_rotation)
        q2 = sw.quaternion_from_matrix(goal_rotation)
        intermediate_goal = sw.diffable_slerp(q1, q2, capped_angle)
        axis, angle = sw.axis_angle_from_quaternion(*sw.quaternion_diff(q1, intermediate_goal))
        r_rot_control = axis * angle

        hack = sw.rotation_matrix_from_axis_angle([0, 0, 1], 0.0001)
        axis, angle = sw.diffable_axis_angle_from_matrix((current_rotation.T * (current_evaluated_rotation * hack)).T)
        c_aa = (axis * angle)

        soft_constraints[str(self) + u'/0'] = SoftConstraint(lower=r_rot_control[0],
                                                             upper=r_rot_control[0],
                                                             weight=weight,
                                                             expression=c_aa[0])
        soft_constraints[str(self) + u'/1'] = SoftConstraint(lower=r_rot_control[1],
                                                             upper=r_rot_control[1],
                                                             weight=weight,
                                                             expression=c_aa[1])
        soft_constraints[str(self) + u'/2'] = SoftConstraint(lower=r_rot_control[2],
                                                             upper=r_rot_control[2],
                                                             weight=weight,
                                                             expression=c_aa[2])
        return soft_constraints


class LinkToAnyAvoidance(Constraint):
    repel_speed = u'repel_speed'
    max_weight_distance = u'max_weight_distance'
    low_weight_distance = u'low_weight_distance'
    zero_weight_distance = u'zero_weight_distance'
    root_T_link_b = u'root_T_link_b'
    link_in_chain = u'link_in_chain'
    A = u'A'
    B = u'B'
    C = u'C'

    def __init__(self, god_map, link_name, repel_speed=1, max_weight_distance=0.0, low_weight_distance=0.02,
                 zero_weight_distance=0.05):
        super(LinkToAnyAvoidance, self).__init__(god_map)
        self.link_name = link_name
        self.robot_root = self.get_robot().get_root()
        self.robot_name = self.get_robot_unsafe().get_name()
        x = np.array([max_weight_distance, low_weight_distance, zero_weight_distance])
        y = np.array([MAX_WEIGHT, LOW_WEIGHT, ZERO_WEIGHT])
        (A, B, C), _ = curve_fit(lambda t, a, b, c: a / (t + c) + b, x, y)

        identity = np.eye(4)

        cpi_identifier = identifier.closest_point[0]
        world_identifier = identifier.world[0]
        data = god_map._data
        robot = data[world_identifier].robot
        get_fk = robot.get_fk_np
        get_chain = robot.get_chain

        
        def root_T_link_b():
            cpi = data[cpi_identifier][self.link_name]
            if cpi.body_b == self.robot_name:
                return get_fk(self.robot_root, cpi.link_b)
            return identity

        
        def link_in_chain(link_name):
            cpi = data[cpi_identifier][self.link_name]
            body_b = cpi.body_b
            if body_b == self.robot_name:
                link_a = cpi.link_a
                link_b = cpi.link_b
                chain = get_chain(link_b, link_a, joints=False)
                return int(link_name in chain)
            return 1

        params = {self.repel_speed: repel_speed,
                  self.max_weight_distance: max_weight_distance,
                  self.low_weight_distance: low_weight_distance,
                  self.zero_weight_distance: zero_weight_distance,
                  self.link_in_chain: link_in_chain,
                  self.root_T_link_b: root_T_link_b,
                  self.A: A,
                  self.B: B,
                  self.C: C}
        self.save_params_on_god_map(params)

    def get_distance_to_closest_object(self, link_name):
        return self.get_god_map().to_symbol(identifier.closest_point + [link_name, u'min_dist'])

    def get_contact_normal_on_b(self, link_name):
        return Vector3Input(self.god_map.to_symbol,
                            prefix=identifier.closest_point + [link_name, u'contact_normal']).get_expression()

    def get_closest_point_on_a(self, link_name):
        return Point3Input(self.god_map.to_symbol,
                           prefix=identifier.closest_point + [link_name, u'position_on_a']).get_expression()

    def get_closest_point_on_b(self, link_name):
        return Point3Input(self.god_map.to_symbol,
                           prefix=identifier.closest_point + [link_name, u'position_on_b']).get_expression()

    def get_actual_distance(self, link_name):
        return self.god_map.to_symbol(identifier.closest_point + [link_name, u'contact_distance'])

    def get_is_link_in_chain_symbol(self, link_name):
        return self.god_map.to_symbol(self.get_identifier() + [self.link_in_chain, (link_name,)])

    def get_root_T_link_b(self):
        return FrameInput(self.god_map.to_symbol, self.get_identifier() + [self.root_T_link_b, tuple()]).get_frame()

    def get_constraint(self):
        soft_constraints = OrderedDict()

        root_T_link = self.get_root_T_link_b()
        chain = self.get_robot().get_chain(self.get_robot().get_root(), self.link_name, False, True, False)
        for i in range(len(chain) - 1):
            l1 = chain[i]
            l2 = chain[i + 1]
            link_in_chain = self.get_is_link_in_chain_symbol(l1)
            fk_expr = self.get_fk(l1, l2)
            root_T_link *= sw.if_eq_zero(link_in_chain, sw.eye(4), fk_expr)
            # add_debug_constraint(soft_constraints, str(self)+'/link in chain / '+l1, link_in_chain)

        point_on_link = self.get_closest_point_on_a(self.link_name)
        other_point = self.get_closest_point_on_b(self.link_name)
        contact_normal = self.get_contact_normal_on_b(self.link_name)
        actual_distance = self.get_actual_distance(self.link_name)
        repel_speed = self.get_input_float(self.repel_speed)
        max_weight_distance = self.get_input_float(self.max_weight_distance)
        zero_weight_distance = self.get_input_float(self.zero_weight_distance)
        A = self.get_input_float(self.A)
        B = self.get_input_float(self.B)
        C = self.get_input_float(self.C)

        controllable_point = root_T_link * point_on_link

        dist = (contact_normal.T * (controllable_point - other_point))[0]

        weight_f = sw.Piecewise([MAX_WEIGHT, actual_distance <= max_weight_distance],
                                [ZERO_WEIGHT, actual_distance > zero_weight_distance],
                                [A / (actual_distance + C) + B, True])

        soft_constraints[str(self)] = SoftConstraint(lower=repel_speed,
                                                     upper=repel_speed,
                                                     weight=weight_f,
                                                     expression=dist)
        # add_debug_constraint(soft_constraints, str(self)+'/dist', dist)
        # add_debug_constraint(soft_constraints, str(self)+'/actual_distance', actual_distance)
        # add_debug_constraint(soft_constraints, str(self)+'/f', A / (actual_distance + C) + B)
        # add_debug_constraint(soft_constraints, str(self)+'/contact_normal x', contact_normal[0])
        # add_debug_constraint(soft_constraints, str(self)+'/contact_normal y', contact_normal[1])
        # add_debug_constraint(soft_constraints, str(self)+'/contact_normal z', contact_normal[2])

        # add_debug_constraint(soft_constraints, str(self)+'/a x', controllable_point[0])
        # add_debug_constraint(soft_constraints, str(self)+'/a y', controllable_point[1])
        # add_debug_constraint(soft_constraints, str(self)+'/a z', controllable_point[2])

        # add_debug_constraint(soft_constraints, str(self)+'/b x', other_point[0])
        # add_debug_constraint(soft_constraints, str(self)+'/b y', other_point[1])
        # add_debug_constraint(soft_constraints, str(self)+'/b z', other_point[2])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a 0', point_on_link[0])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a 1', point_on_link[1])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a 2', point_on_link[2])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a root 0', controllable_point[0])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a root 1', controllable_point[1])
        # add_debug_constraint(soft_constraints, str(self)+'/p on a root 2', controllable_point[2])
        return soft_constraints

    def __str__(self):
        return u'{}/{}'.format(self.__class__.__name__, self.link_name)


class AlignPlanes(Constraint):
    root_normal = u'root_normal'
    tip_normal = u'tip_normal'
    weight = u'weight'
    gain = u'gain'
    max_speed = u'max_speed'

    def __init__(self, god_map, root, tip, root_normal, tip_normal, weight=HIGH_WEIGHT, gain=3, max_speed=0.5):
        """
        :type god_map:
        :type root: str
        :type tip: str
        :type root_normal: Vector3Stamped
        :type tip_normal: Vector3Stamped
        """
        super(AlignPlanes, self).__init__(god_map)
        self.root = root
        self.tip = tip

        root_normal = convert_dictionary_to_ros_message(u'geometry_msgs/Vector3Stamped', root_normal)
        root_normal = transform_vector(self.root, root_normal)
        tmp = np.array([root_normal.vector.x, root_normal.vector.y, root_normal.vector.z])
        tmp = tmp / np.linalg.norm(tmp)
        root_normal.vector = Vector3(*tmp)

        tip_normal = convert_dictionary_to_ros_message(u'geometry_msgs/Vector3Stamped', tip_normal)
        tip_normal = transform_vector(self.tip, tip_normal)
        tmp = np.array([tip_normal.vector.x, tip_normal.vector.y, tip_normal.vector.z])
        tmp = tmp / np.linalg.norm(tmp)
        tip_normal.vector = Vector3(*tmp)

        params = {self.root_normal: root_normal,
                  self.tip_normal: tip_normal,
                  self.weight: weight,
                  self.gain: gain,
                  self.max_speed: max_speed}
        self.save_params_on_god_map(params)

    def __str__(self):
        return u'{}/{}/{}'.format(self.__class__.__name__, self.root, self.tip)

    def get_root_normal_vector(self):
        return self.get_input_Vector3Stamped(self.root_normal)

    def get_tip_normal_vector(self):
        return self.get_input_Vector3Stamped(self.tip_normal)

    def get_constraint(self):
        # TODO integrate gain and max_speed?
        soft_constraints = OrderedDict()
        weight = self.get_input_float(self.weight)
        gain = self.get_input_float(self.gain)
        max_speed = self.get_input_float(self.max_speed)
        root_R_tip = sw.rotation_of(self.get_fk(self.root, self.tip))
        tip_normal__tip = self.get_tip_normal_vector()
        root_normal__root = self.get_root_normal_vector()

        tip_normal__root = root_R_tip * tip_normal__tip
        diff = root_normal__root - tip_normal__root

        soft_constraints[str(self) + u'x'] = SoftConstraint(lower=diff[0],
                                                            upper=diff[0],
                                                            weight=weight,
                                                            expression=tip_normal__root[0])
        soft_constraints[str(self) + u'y'] = SoftConstraint(lower=diff[1],
                                                            upper=diff[1],
                                                            weight=weight,
                                                            expression=tip_normal__root[1])
        soft_constraints[str(self) + u'z'] = SoftConstraint(lower=diff[2],
                                                            upper=diff[2],
                                                            weight=weight,
                                                            expression=tip_normal__root[2])
        return soft_constraints


class GravityJoint(Constraint):
    weight = u'weight'

    def __init__(self, god_map, joint_name, object_name, weight=MAX_WEIGHT):
        super(GravityJoint, self).__init__(god_map)
        self.joint_name = joint_name
        self.weight = weight
        self.object_name = object_name
        params = {self.weight: weight}
        self.save_params_on_god_map(params)

    def get_constraint(self):
        soft_constraints = OrderedDict()

        current_joint = self.get_input_joint_position(self.joint_name)
        weight = self.get_input_float(self.weight)

        parent_link = self.get_robot().get_parent_link_of_joint(self.joint_name)

        parent_R_root = sw.rotation_of(self.get_fk(parent_link, self.get_robot().get_root()))

        com__parent = sw.position_of(self.get_fk_evaluated(parent_link, self.object_name))
        com__parent[3] = 0
        com__parent = sw.scale(com__parent, 1)

        g = sw.vector3(0, 0, -1)
        g__parent = parent_R_root * g
        axis_of_rotation = sw.vector3(*self.get_robot().get_joint_axis(self.joint_name))
        l = sw.dot(g__parent, axis_of_rotation)
        goal__parent = g__parent - sw.scale(axis_of_rotation, l)
        goal__parent = sw.scale(goal__parent, 1)

        goal_vel = sw.acos(sw.dot(com__parent, goal__parent))

        ref_axis_of_rotation = sw.cross(com__parent, goal__parent)
        goal_vel *= sw.sign(sw.dot(ref_axis_of_rotation, axis_of_rotation))

        soft_constraints[str(self)] = SoftConstraint(lower=goal_vel,  # sw.Min(goal_vel, 0),
                                                     upper=goal_vel,  # sw.Max(goal_vel, 0),
                                                     weight=weight,
                                                     expression=current_joint)

        return soft_constraints

    def __str__(self):
        return u'{}/{}'.format(self.__class__.__name__, self.joint_name)


def add_debug_constraint(d, key, expr):
    """
    If you want to see an arbitrary evaluated expression in the matrix use this.
    These softconstraints will not influence anything.
    :param d: a dict where the softcontraint will be added to
    :type: dict
    :param key: a name to identify the debug soft contraint
    :type key: str
    :type expr: sw.Symbol
    """
    d[key] = SoftConstraint(lower=expr,
                            upper=expr,
                            weight=0,
                            expression=1)