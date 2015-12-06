"""Implement the GeometricProgram class"""
import numpy as np

import sys
from time import time

from .variables import Variable, VectorVariable
from .small_classes import CootMatrix, HashVector, KeyDict, KeySet
from .nomial_data import NomialData
from .nomials import Posynomial
from .small_classes import SolverLog

from .small_scripts import is_sweepvar


class GeometricProgram(NomialData):
    """Standard mathematical representation of a GP.

    Arguments
    ---------
    cost : Constraint
        Posynomial to minimize when solving
    constraints : list of Posynomials
        Constraints to maintain when solving (implicitly Posynomials <= 1)
        GeometricProgram does not accept equality constraints (e.g. x == 1);
         instead use two inequality constraints (e.g. x <= 1, 1/x <= 1)
    verbosity : int (optional)
        If verbosity is greater than zero, warns about missing bounds
        on creation.

    Attributes with side effects
    ----------------------------
    `solver_out` and `solver_log` are set during a solve
    `result` is set at the end of a solve

    Examples
    --------
    >>> gp = gpkit.geometric_program.GeometricProgram(
                        # minimize
                        x,
                        [   # subject to
                            1/x  # <= 1, implicitly
                        ])
    >>> gp.solve()
    """

    def __init__(self, cost, constraints, substitutions=None, verbosity=1):
        self.cost = cost
        self.constraints = constraints
        cost = cost.sub(cost.values)
        subbedcost = cost.sub(substitutions) if substitutions else cost
        self.posynomials = [subbedcost]
        self.constr_idxs = []
        for constraint in constraints:
            if substitutions:
                constraint.substitutions.update(substitutions)
            constr_posys = constraint.as_posyslt1()
            if not all(constr_posys):
                raise ValueError("%s is an invalid constraint for a"
                                 " GeometricProgram" % constraint)
            self.constr_idxs.append(range(len(self.posynomials),
                                    len(self.posynomials)+len(constr_posys)))
            self.posynomials.extend(constr_posys)
        # init NomialData to create self.exps, self.cs, and so on
        super(GeometricProgram, self).init_from_nomials(self.posynomials)
        if self.any_nonpositive_cs:
            raise ValueError("GeometricPrograms cannot contain Signomials.")
        unsubbed = [k for k, v in self.values.items() if not is_sweepvar(v)]
        # k [j]: number of monomials (columns of F) present in each constraint
        self.k = [len(p.cs) for p in self.posynomials]
        # p_idxs [i]: posynomial index of each monomial
        # m_idxs [i]: monomial indices of each posynomial
        p_idxs = []
        m_idxs = []
        for i, p_len in enumerate(self.k):
            m_idxs.append(list(range(len(p_idxs), len(p_idxs) + p_len)))
            p_idxs += [i]*p_len
        self.p_idxs = np.array(p_idxs)
        self.m_idxs = m_idxs

        self.A, self.missingbounds = genA(self.exps, self.varlocs)

        if verbosity > 0:
            for var, bound in sorted(self.missingbounds.items()):
                print("%s has no %s bound" % (var, bound))

    def solve(self, solver=None, verbosity=1, *args, **kwargs):
        """Solves a GeometricProgram and returns the solution.

        Arguments
        ---------
        solver : str or function (optional)
            By default uses one of the solvers found during installation.
            If set to "mosek", "mosek_cli", or "cvxopt", uses that solver.
            If set to a function, passes that function cs, A, p_idxs, and k.
        verbosity : int (optional)
            If greater than 0, prints solver name and solve time.
        *args, **kwargs :
            Passed to solver constructor and solver function.


        Returns
        -------
        result : dict
            A dictionary containing the translated solver result; keys below.

            cost : float
                The value of the objective at the solution.
            variables : dict
                The value of each variable at the solution.
            sensitivities : dict
                monomials : array of floats
                    Each monomial's dual variable value at the solution.
                posynomials : array of floats
                    Each posynomials's dual variable value at the solution.
        """
        if solver is None:
            from . import settings
            if settings['installed_solvers']:
                solver = settings['installed_solvers'][0]
            else:
                raise ValueError("No solver was given; perhaps gpkit was not"
                                 " properly installed, or found no solvers"
                                 " during the installation process.")

        if solver == 'cvxopt':
            from ._cvxopt import cvxoptimize_fn
            solverfn = cvxoptimize_fn(*args, **kwargs)
        elif solver == "mosek_cli":
            from ._mosek import cli_expopt
            solverfn = cli_expopt.imize_fn(*args, **kwargs)
        elif solver == "mosek":
            from ._mosek import expopt
            solverfn = expopt.imize
        elif hasattr(solver, "__call__"):
            solverfn = solver
            solver = solver.__name__
        else:
            raise ValueError("Unknown solver '%s'." % solver)

        if verbosity > 0:
            print("Using solver '%s'" % solver)
            print("Solving for %i variables." % len(self.varlocs))
            tic = time()

        original_stdout = sys.stdout
        # NOTE: SIDE EFFECTS
        self.solver_log = SolverLog(verbosity-1, original_stdout)
        try:
            sys.stdout = self.solver_log   # CAPTURING STDOUT
            solver_out = solverfn(c=self.cs, A=self.A, p_idxs=self.p_idxs,
                                  k=self.k, *args, **kwargs)
        finally:
            sys.stdout = original_stdout   # RETURNING STDOUT
        self.solver_out = solver_out   # END SIDE EFFECTS

        if verbosity > 0:
            soltime = time() - tic
            print("Solving took %.3g seconds." % (soltime,))
            tic = time()

        result = {}
        # confirm lengths before calling zip
        primal = np.ravel(solver_out['primal'])
        assert len(self.varlocs) == len(primal)
        result["freevariables"] = dict(zip(self.varlocs, np.exp(primal)))
        result["variables"] = result["freevariables"]

        if "objective" in solver_out:
            result["cost"] = float(solver_out["objective"])
        else:
            # use self.posynomials[0] becaues cost has been subbed
            result["cost"] = self.posynomials[0].subsummag(result["variables"])

        result["sensitivities"] = {}
        if "nu" in solver_out:
            nu = np.ravel(solver_out["nu"])
            la = np.array([sum(nu[self.p_idxs == i])
                           for i in range(len(self.posynomials))])
        elif "la" in solver_out:
            la = np.ravel(solver_out["la"])
            if len(la) == len(self.posynomials) - 1:
                # assume the cost's sensitivity has been dropped
                la = np.hstack(([1.0], la))
            Ax = np.ravel(self.A.dot(solver_out['primal']))
            z = Ax + np.log(self.cs)
            m_iss = [self.p_idxs == i for i in range(len(la))]
            nu = np.hstack([la[p_i]*np.exp(z[m_is])/sum(np.exp(z[m_is]))
                            for p_i, m_is in enumerate(m_iss)])
        else:
            raise RuntimeWarning("The dual solution was not returned.")

        result["sensitivities"] = {"constraints": {}}
        # initialized the var_senss dict with constants in the subbed cost
        var_senss = {var: sum([self.cost.exps[i][var]*nu[i] for i in locs])
                     for (var, locs) in self.cost.varlocs.items()
                     if (var in self.cost.varlocs
                         and var not in self.posynomials[0].varlocs)}
        var_senss = HashVector(var_senss)
        for c_i, posy_idxs in enumerate(self.constr_idxs):
            constr = self.constraints[c_i]
            p_senss = [la[p_i] for p_i in posy_idxs]
            m_sensss = [[nu[i] for i in self.m_idxs[p_i]] for p_i in posy_idxs]
            constr_sens, p_var_senss = constr.sens_from_dual(p_senss, m_sensss)
            result["sensitivities"]["constraints"][str(constr)] = constr_sens
            var_senss += p_var_senss
        result["sensitivities"]["constants"] = KeyDict(var_senss)

        result["constants"] = {}
        for constraint in self.constraints:
            for dictionary in [result["constants"], result["variables"]]:
                dictionary.update(constraint.substitutions)
        for key in ["freevariables", "variables", "constants"]:
            result[key] = KeyDict(result[key])
            result[key].bake()

        for constraint in self.constraints:
            if hasattr(constraint, "process_result"):
                constraint.process_result(result)

        self.result = result  # NOTE: SIDE EFFECTS

        if verbosity > 1:
            print("result packing took %.2g%% of solve time" %
                  ((time() - tic) / soltime * 100))
            tic = time()

        if solver_out.get("status", None) not in ["optimal", "OPTIMAL"]:
            raise RuntimeWarning("final status of solver '%s' was '%s', "
                                 "not 'optimal'." %
                                 (solver, solver_out.get("status", None)) +
                                 "\n\nThe infeasible solve's result is stored"
                                 " in the 'result' attribute"
                                 " (model.program.result)"
                                 " and its raw output in 'solver_out'."
                                 " If the problem was Primal Infeasible,"
                                 " you can generate a feasibility-finding"
                                 " relaxation of your Model with"
                                 " model.feasibility().")

        self.check_solution(result["cost"], primal, nu, la)

        if verbosity > 1:
            print("solution checking took %.2g%% of solve time" %
                  ((time() - tic) / soltime * 100))
        return result

    def check_solution(self, cost, primal, nu, la, tol=1e-5):
        """Run a series of checks to mathematically confirm sol solves this GP

        Arguments
        ---------
        cost:   float
            cost returned by solver
        primal: list
            primal solution returned by solver
        nu:     numpy.ndarray
            monomial lagrange multiplier
        la:     numpy.ndarray
            posynomial lagrange multiplier

        Raises
        ------
        RuntimeWarning, if any problems are found
        """
        def _almost_equal(num1, num2):
            "local almost equal test"
            return num1 == num2 or abs((num1 - num2) / (num1 + num2)) < tol
        A = self.A.tocsr()
        # check primal sol
        primal_exp_vals = self.cs * np.exp(A.dot(primal))   # c*e^Ax
        if not _almost_equal(primal_exp_vals[self.m_idxs[0]].sum(), cost):
            raise RuntimeWarning("Primal solution computed cost did not match"
                                 " solver-returned cost: %s vs %s" %
                                 (primal_exp_vals[self.m_idxs[0]].sum(), cost))
        for mi in self.m_idxs[1:]:
            if primal_exp_vals[mi].sum() > 1 + tol:
                raise RuntimeWarning("Primal solution violates constraint:"
                                     " %s is greater than 1." %
                                     primal_exp_vals[mi].sum())
        # check dual sol
        # note: follows dual formulation in section 3.1 of
        # http://web.mit.edu/~whoburg/www/papers/hoburg_phd_thesis.pdf
        nu0 = nu[self.m_idxs[0]]
        if not _almost_equal(nu0.sum(), 1.):
            raise RuntimeWarning("Dual variables associated with objective"
                                 " sum to %s, not 1" % nu0.sum())
        if any(nu < 0):
            raise RuntimeWarning("Dual solution has negative entries")
        nuA = A.T.dot(nu)
        if any(np.abs(nuA) > tol):
            raise RuntimeWarning("sum of nu^T * A did not vanish")
        b = np.log(self.cs)
        dual_cost = sum(nu[mi].dot(b[mi]) -
                        (nu[mi].dot(np.log(nu[mi]/la[i])) if la[i] else 0)
                        for i, mi in enumerate(self.m_idxs))
        if not _almost_equal(np.exp(dual_cost), cost):
            raise RuntimeWarning("Dual cost %s does not match primal"
                                 " cost %s" % (np.exp(dual_cost), cost))

    def __repr__(self):
        return "gpkit.%s(\n%s)" % (self.__class__.__name__, str(self))

    def __str__(self):
        """String representation of a GeometricProgram.

        Contains all of its parameters."""
        return "\n".join(["  # minimize",
                          "    %s," % self.cost,
                          "[ # subject to"] +
                         ["    %s," % constr
                          for constr in self.constraints] +
                         [']'])

    def latex(self):
        """LaTeX representation of a GeometricProgram.

        Contains all of its parameters."""
        return "\n".join(["\\begin{array}[ll]",
                          "\\text{}",
                          "\\text{minimize}",
                          "    & %s \\\\" % self.cost.latex(),
                          "\\text{subject to}"] +
                         ["    & %s \\\\" % constr.latex()
                          for constr in self.constraints] +
                         ["\\end{array}"])

    def _repr_latex_(self):
        return "$$"+self.latex()+"$$"


def genA(exps, varlocs):
    """Generates A matrix from exps and varlocs

    Arguments
    ---------
        exps : list of Hashvectors
            Exponents for each monomial in a GP
        varlocs : dict
            Locations of each variable in exps

    Returns
    -------
        A : sparse Cootmatrix
            Exponents of the various free variables for each monomial: rows
            of A are monomials, columns of A are variables.
        missingbounds : dict
            Keys: variables that lack bounds. Values: which bounds are missed.
    """

    missingbounds = {}
    A = CootMatrix([], [], [])
    for j, var in enumerate(varlocs):
        varsign = "both" if "value" in var.descr else None
        for i in varlocs[var]:
            exp = exps[i][var]
            A.append(i, j, exp)
            if varsign is "both":
                pass
            elif varsign is None:
                varsign = np.sign(exp)
            elif np.sign(exp) != varsign:
                varsign = "both"

        if varsign != "both":
            if varsign == 1:
                bound = "lower"
            elif varsign == -1:
                bound = "upper"
            else:
                # just being safe
                raise RuntimeWarning("Unexpected varsign %s" % varsign)
            missingbounds[var] = bound

    # add constant terms
    for i, exp in enumerate(exps):
        if not exp:
            A.append(i, 0, 0)

    A.update_shape()

    return A, missingbounds
