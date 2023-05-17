import numpy as np
import math
from .dg1d import *
from .spatialDiscretization import *
from .mesh1d import Mesh1D

class Maxwell1D(SpatialDiscretization):
    def __init__(self, n_order: int, mesh: Mesh1D, fluxType="Upwind"):
        assert n_order > 0
        assert mesh.number_of_elements() > 0

        self.mesh = mesh
        self.n_order = n_order
        self.fluxType = fluxType

        self.n_faces = 2
        self.n_fp = 1

        alpha = 0
        beta = 0

        # Set up material parameters
        self.epsilon = np.ones(mesh.number_of_elements())
        self.mu = np.ones(mesh.number_of_elements())

        r = jacobiGL(alpha, beta, n_order)
        jacobi_p = jacobi_polynomial(r, alpha, beta, n_order)
        vander = vandermonde(n_order, r)
        self.x = nodes_coordinates(n_order, mesh.EToV, mesh.vx)
        self.nx = normals(mesh.number_of_elements())

        etoe, etof = connect(mesh.EToV)
        self.vmap_m, self.vmap_p, self.vmap_b, self.map_b = build_maps(
            n_order, self.x, etoe, etof)

        self.fmask_1 = np.where(np.abs(r+1) < 1e-10)[0][0]
        self.fmask_2 = np.where(np.abs(r-1) < 1e-10)[0][0]
        self.fmask = [self.fmask_1, self.fmask_2]

        self.lift = surface_integral_dg(n_order, vander)
        self.diff_matrix = differentiation_matrix(n_order, r, vander)
        self.rx, self.jacobian = geometric_factors(self.x, self.diff_matrix)

        K = self.mesh.number_of_elements()
        Z_imp = self.get_impedance()

        self.Z_imp_m = Z_imp.transpose().take(self.vmap_m)
        self.Z_imp_p = Z_imp.transpose().take(self.vmap_p)
        self.Z_imp_m = self.Z_imp_m.reshape(
            self.n_fp*self.n_faces, K, order='F')
        self.Z_imp_p = self.Z_imp_p.reshape(
            self.n_fp*self.n_faces, K, order='F')

        self.Y_imp_m = 1.0 / self.Z_imp_m
        self.Y_imp_p = 1.0 / self.Z_imp_p

        self.Z_imp_sum = self.Z_imp_m + self.Z_imp_p
        self.Y_imp_sum = self.Y_imp_m + self.Y_imp_p

    def number_of_nodes_per_element(self):
        return self.n_order + 1

    def get_nodes(self):
        return set_nodes(self.n_order, self.mesh.vx[self.mesh.EToV])

    def get_minimum_node_distance(self):
        return min(np.abs(self.x[0, :] - self.x[1, :]))
    
    def buildFields(self):
        E = np.zeros([self.number_of_nodes_per_element(),
                          self.mesh.number_of_elements()])
        H = np.zeros(E.shape)

        return E, H

    def get_impedance(self):
        Z_imp = np.zeros(self.x.shape)
        for i in range(Z_imp.shape[1]):
            Z_imp[:, i] = np.sqrt(self.mu[i] / self.epsilon[i])

        return Z_imp

    def fieldsOnBoundaryConditions(self, E, H):
        bcType = self.mesh.boundary_label
        if bcType == "PEC":
            Ebc = - E.transpose().take(self.vmap_b)
            Hbc = H.transpose().take(self.vmap_b)
        elif bcType == "PMC":
            Hbc = - H.transpose().take(self.vmap_b)
            Ebc = E.transpose().take(self.vmap_b)
        elif bcType == "SMA":
            Hbc = H.transpose().take(self.vmap_b) * 0.0
            Ebc = E.transpose().take(self.vmap_b) * 0.0 
        elif bcType == "Periodic":
            Ebc = E.transpose().take(self.vmap_b[::-1])
            Hbc = H.transpose().take(self.vmap_b[::-1])
        else:
            raise ValueError("Invalid boundary label.")
        return Ebc, Hbc

    def computeFlux(self):
        if self.fluxType == "Upwind":
            flux_E = 1/self.Z_imp_sum*(self.nx*self.Z_imp_p*self.dH-self.dE)
            flux_H = 1/self.Y_imp_sum*(self.nx*self.Y_imp_p*self.dE-self.dH)
        else:
            flux_E = 1/self.Z_imp_sum*(self.nx*self.Z_imp_p*self.dH)
            flux_H = 1/self.Y_imp_sum*(self.nx*self.Y_imp_p*self.dE)
        return flux_E, flux_H

    def computeJumps(self, Ebc, Hbc, E, H):
        dE = E.transpose().take(self.vmap_m) - E.transpose().take(self.vmap_p)
        dH = H.transpose().take(self.vmap_m) - H.transpose().take(self.vmap_p)
        dE[self.map_b] = E.transpose().take(self.vmap_b)-Ebc
        dH[self.map_b] = H.transpose().take(self.vmap_b)-Hbc
        dE = dE.reshape(self.n_fp*self.n_faces,
                             self.mesh.number_of_elements(), order='F')
        dH = dH.reshape(self.n_fp*self.n_faces,
                             self.mesh.number_of_elements(), order='F')
        return dE, dH

    def computeRHS(self, E, H):

        Ebc, Hbc = self.fieldsOnBoundaryConditions(E, H)
        self.dE, self.dH = self.computeJumps(Ebc, Hbc, E, H)

        flux_E, flux_H = self.computeFlux()

        # Compute right hand sides of the PDE’s
        f_scale = 1/self.jacobian[self.fmask]
        rhs_drH = np.matmul(self.diff_matrix, H)
        rhs_drE = np.matmul(self.diff_matrix, E)
        rhs_E = 1/self.epsilon * \
            (np.multiply(-1*self.rx, rhs_drH) +
             np.matmul(self.lift, f_scale * flux_E))
        rhs_H = 1/self.mu * (np.multiply(-1*self.rx, rhs_drE) +
                             np.matmul(self.lift, f_scale * flux_H))

        return rhs_E, rhs_H
    
    def buildEvolutionOperator(self):
        Np = self.number_of_nodes_per_element()
        K = self.mesh.number_of_elements()
        N = 2 * Np * K
        A = np.zeros((N,N))
        for i in range(N):
            E, H = self.buildFields()
            node = i % Np
            elem = int(np.floor(i / Np)) % K
            if i < N/2:
                E[node, elem] = 1.0
            else:
                H[node, elem] = 1.0
            rhsE, rhsH = self.computeRHS(E, H)
            q0 = np.vstack([rhsE.reshape(Np*K,1,order='F'), rhsH.reshape(Np*K,1, order='F')])
            A[:,i] = q0[:,0]

        return A

