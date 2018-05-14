from collections import OrderedDict, namedtuple

import numpy as np
from time import time

from giskardpy import BACKEND

import giskardpy.symengine_wrappers as spw
from giskardpy.qp_solver import QPSolver

SoftConstraint = namedtuple('SoftConstraint', ['lower', 'upper', 'weight', 'expression'])
HardConstraint = namedtuple('HardConstraint', ['lower', 'upper', 'expression'])
JointConstraint = namedtuple('JointConstraint', ['lower', 'upper', 'weight'])

BIG_NUMBER = 1e9

class QProblemBuilder(object):
    def __init__(self, joint_constraints_dict, hard_constraints_dict, soft_constraints_dict, controlled_joint_symbols,
                 free_symbols=None):
        assert(not len(controlled_joint_symbols) > len(joint_constraints_dict))
        assert(not len(controlled_joint_symbols) < len(joint_constraints_dict))
        assert(len(hard_constraints_dict) <= len(controlled_joint_symbols))
        self.free_symbols = free_symbols
        self.joint_constraints_dict = joint_constraints_dict
        self.hard_constraints_dict = hard_constraints_dict
        self.soft_constraints_dict = soft_constraints_dict
        self.controlled_joints = controlled_joint_symbols
        self.controlled_joints_strs = [str(x) for x in self.controlled_joints]
        self.make_sympy_matrices()

        self.qp_solver = QPSolver(self.H.shape[0], len(self.lbA))

    def make_sympy_matrices(self):
        print('building new controller')
        # TODO cpu intensive
        weights = []
        lb = []
        ub = []
        lbA = []
        ubA = []
        soft_expressions = []
        hard_expressions = []
        for c in self.joint_constraints_dict.values():
            weights.append(c.weight)
            lb.append(c.lower)
            ub.append(c.upper)
        for c in self.hard_constraints_dict.values():
            lbA.append(c.lower)
            ubA.append(c.upper)
            hard_expressions.append(c.expression)
        for k, c in self.soft_constraints_dict.items():
            weights.append(c.weight)
            lbA.append(c.lower)
            ubA.append(c.upper)
            lb.append(-BIG_NUMBER)
            ub.append(BIG_NUMBER)
            soft_expressions.append(c.expression)

        self.H = spw.diag(*weights)

        self.np_g = np.zeros(len(weights))

        self.lb = spw.Matrix(lb)
        self.ub = spw.Matrix(ub)

        # make A
        # hard part
        M_controlled_joints = spw.Matrix(self.controlled_joints)
        A_hard = spw.Matrix(hard_expressions)
        A_hard = A_hard.jacobian(M_controlled_joints)
        zerosHxS = spw.zeros(A_hard.shape[0], len(soft_expressions))
        A_hard = A_hard.row_join(zerosHxS)

        # soft part
        A_soft = spw.Matrix(soft_expressions)
        t = time()
        A_soft = A_soft.jacobian(M_controlled_joints)
        # print('jacobian took {}'.format(time() - t))
        identity = spw.eye(A_soft.shape[0])
        A_soft = A_soft.row_join(identity)

        # final A
        self.A = A_hard.col_join(A_soft)

        self.lbA = spw.Matrix(lbA)
        self.ubA = spw.Matrix(ubA)

        big_ass_M_A = self.A.row_join(self.lbA).row_join(self.ubA)
        big_ass_M_H = self.H.row_join(self.lb).row_join(self.ub)
        # putting everything into one big matrix to take full advantage of cse in speed_up()
        self.big_ass_M = big_ass_M_A.col_join(big_ass_M_H)

        t = time()
        if self.free_symbols is None:
            self.free_symbols = self.big_ass_M.free_symbols
        self.cython_big_ass_M = spw.speed_up(self.big_ass_M, self.free_symbols, backend=BACKEND)

        print('new controller ready')

    def get_cmd(self, substitutions):
        """

        :param substitutions: symbol -> value
        :type substitutions: dict
        :return: joint name -> joint command
        :rtype: dict
        """
        np_big_ass_M = self.cython_big_ass_M(**substitutions)
        np_H = np.array(np_big_ass_M[self.A.shape[0]:,:-2])
        np_A = np.array(np_big_ass_M[:self.A.shape[0],:self.A.shape[1]])
        np_lb = np.array(np_big_ass_M[self.A.shape[0]:,-2])
        np_ub = np.array(np_big_ass_M[self.A.shape[0]:,-1])
        np_lbA = np.array(np_big_ass_M[:self.A.shape[0],-2])
        np_ubA = np.array(np_big_ass_M[:self.A.shape[0],-1])
        xdot_full = self.qp_solver.solve(np_H, self.np_g, np_A, np_lb, np_ub, np_lbA, np_ubA)
        if xdot_full is None:
            return None
        return OrderedDict((observable, xdot_full[i]) for i, observable in enumerate(self.controlled_joints_strs))