#!/usr/bin/python

import numpy as np
import scipy.linalg as sla
from numpy import sqrt,einsum
import pyscf
from pyscf import gto, scf, mp, cc, ao2mo, df, lib
# from pyscf.mp import dfmp2_testing #(work)
from pyscf.mp import dfmp2 #(work) testing
# from pyscf.mp import dfmp2       #(home)

#from pyscf.mp.mp2 import make_rdm1, make_rdm2

''' This is a working version of DF-MP2 modification to the MP2 code
    The integrals need to calculated on the fly, without storing them
    POSTMAT: this version has correct rdms, but wrong input parameters.
    need to change all mp2. to correct inputs
'''
    #TODO check the eris

def solve (mol, nel, cf_core, cf_gs, ImpOrbs, chempot=0., n_orth=0, FrozenPot=None, mf_tot=None):
    # cf_core : core orbitals (in AO basis, assumed orthonormal)
    # cf_gs   : guess orbitals (in AO basis)
    # ImpOrbs : cf_gs -> impurity orbitals transformation
    # n_orth  : number of orthonormal orbitals in cf_gs [1..n_orth]

    mol_ = gto.Mole()
    mol_.build (verbose=0)
    mol_.nelectron = nel
    mol_.incore_anyway = True

    cfx = cf_gs
    print("cfx shape", cfx.shape)
    Sf  = mol.intor_symmetric('cint1e_ovlp_sph')
    Hc  = mol.intor_symmetric('cint1e_kin_sph') \
        + mol.intor_symmetric('cint1e_nuc_sph') \
        + FrozenPot

    occ = np.zeros((cfx.shape[1],))
    occ[:nel//2] = 2.

    # core contributions
    dm_core = np.dot(cf_core, cf_core.T)*2
    jk_core = scf.hf.get_veff (mol, dm_core)
    e_core  =     np.trace(np.dot(Hc, dm_core)) \
            + 0.5*np.trace(np.dot(jk_core, dm_core))

    # transform integrals
    Sp = np.dot(cfx.T, np.dot(Sf, cfx))
    Hp = np.dot(cfx.T, np.dot(Hc, cfx))
    jkp = np.dot(cfx.T, np.dot(jk_core, cfx))


    # density fitting ============================================================
    # mf = scf.RHF(mol).density_fit()     # this should be moved out of to the parent directory, to avoid repetition
    # mf.with_df._cderi_to_save = 'saved_cderi.h5' # rank-3 decomposition
    # mf.kernel()                    ### moved these three lines to orbital_selection_fc

    auxmol = df.incore.format_aux_basis(mol, auxbasis='weigend')
    j3c    = df.incore.aux_e2(mol, auxmol, intor='cint3c2e_sph', aosym='s1')
    nao    = mol.nao_nr()
    naoaux = auxmol.nao_nr()
    j3c    = j3c.reshape(nao,nao,naoaux) # (ij|L)
    print("j3c shape", j3c.shape)
    j2c    = df.incore.fill_2c2e(mol, auxmol) #(L|M) overlap matrix between auxiliary basis functions

    #the eri is (ij|kl) = \sum_LM (ij|L) (L|M) (M|kl)
    omega = sla.inv(j2c)
    eps,U = sla.eigh(omega)
    #after this transformation the eri is (ij|kl) = \sum_L (ij|L) (L|kl)
    j3c   = np.dot(np.dot(j3c,U),np.diag(np.sqrt(eps)))

    #this part is slow, as we again store the whole eri_df
    # conv = np.einsum('prl,pi,rj->ijl', j3c, cfx, cfx)
    conv = np.einsum('prl,pi->irl',j3c,cfx)
    conv = np.einsum('irl,rj->ijl',conv,cfx)
    df_eri = np.einsum('ijm,klm->ijkl',conv,conv)

    intsp_df = ao2mo.restore(4, df_eri, cfx.shape[1])
    print("DF instp", intsp_df.shape)
    # =============================================================================

    # intsp = ao2mo.outcore.full_iofree (mol, cfx)    #TODO: this we need to calculate on the fly using generator f'n
    # print("intsp shape", intsp.shape)

    # orthogonalize cf [virtuals]
    cf  = np.zeros((cfx.shape[1],)*2,)
    if n_orth > 0:
        assert (n_orth <= cfx.shape[1])
        assert (np.allclose(np.eye(n_orth), Sp[:n_orth,:n_orth]))
    else:
        n_orth = 0

    cf[:n_orth,:n_orth] = np.eye(n_orth)
    if n_orth < cfx.shape[1]:
        val, vec = sla.eigh(-Sp[n_orth:,n_orth:])
        idx = -val > 1.e-12
        U = np.dot(vec[:,idx]*1./(np.sqrt(-val[idx])), \
                   vec[:,idx].T)
        cf[n_orth:,n_orth:] = U

    # define ImpOrbs projection
    Xp = np.dot(ImpOrbs, ImpOrbs.T)

    # Si = np.dot(ImpOrbs.T, np.dot(Sp, ImpOrbs))
    # Mp = np.dot(ImpOrbs, np.dot(sla.inv(Si), ImpOrbs.T))
    Np = np.dot(Sp, Xp)
    # print np.allclose(Np, np.dot(Np, np.dot(Mp, Np)))

    # HF calculation
    mol_.energy_nuc = lambda *args: mol.energy_nuc() + e_core
    mf1 = scf.RHF(mol_) #.density_fit()
    #mf.verbose = 4
    mf1.mo_coeff  = cf
    mf1.mo_occ    = occ
    mf1.get_ovlp  = lambda *args: Sp
    mf1.get_hcore = lambda *args: Hp + jkp - 0.5*chempot*(Np + Np.T)
    mf1._eri = ao2mo.restore (8, intsp_df, cfx.shape[1]) #trying something

    nt = scf.newton(mf1)
    #nt.verbose = 4
    nt.max_cycle_inner = 1
    nt.max_stepsize = 0.25
    nt.ah_max_cycle = 32
    nt.ah_start_tol = 1.0e-12
    nt.ah_grad_trust_region = 1.0e8
    nt.conv_tol_grad = 1.0e-6

    nt.kernel()
    cf = nt.mo_coeff
    if not nt.converged:
       raise RuntimeError ('hf failed to converge')
    mo_coeff  = nt.mo_coeff
    mo_energy = nt.mo_energy
    mo_occ    = nt.mo_occ
    print("mo_coeff", mo_coeff)

    # dfMP2 solution
    nocc = nel//2
    # mp2solver = dfmp2_testing.MP2(mf_tot) #(work)  #we just pass the mf for the full molecule to dfmp2
    mp2solver = dfmp2.DFMP2(mf_tot) #(work)  #we just pass the mf for the full molecule to dfmp2
    # mp2solver = dfmp2.MP2(mf)  #(home)
    mp2solver.verbose = 5
    mp2solver.kernel(mo_energy=mo_energy, mo_coeff=mo_coeff, nocc=nocc)
    mp2solver.mo_occ=mo_occ.copy()   # this is DIRTY


    def get_t2(mp):
        '''basically identical to the DFMP2 kernel, returns t2'''
        from pyscf.mp import mp2
        mo_coeff  = mp2._mo_without_core(mp, mp.mo_coeff)
        # print("mo_coeff rdms", mo_coeff)
        mo_energy = mp2._mo_energy_without_core(mp, mp.mo_energy)
        nocc = mp.nocc
        nvir = mp.nmo - nocc
        eia  = mo_energy[:nocc,None] - mo_energy[None,nocc:]
        emp2 = 0
        t2   = []
        for istep, qov in enumerate(mp.loop_ao2mo(mo_coeff, nocc)):
            for i in range(nocc):
                buf = np.dot(qov[:,i*nvir:(i+1)*nvir].T,qov).reshape(nvir,nocc,nvir)
                gi  = np.array(buf,copy=False)
                gi  = gi.reshape(nvir,nocc,nvir).transpose(1,0,2)
                t2i = gi.conj()/lib.direct_sum('jb+a->jba',eia,eia[i])
                t2.append(t2i)
                emp2 += np.einsum('jab,jab',t2i,gi) * 2
                emp2 -= np.einsum('jab,jba',t2i,gi)
        return emp2,t2

    def make_rdm1(mp2solver):
        '''rdm1 in the MO basis'''
        from pyscf.cc import ccsd_rdm
        doo, dvv = _gamma1_intermediates(mp2solver)
        nocc = doo.shape[0]
        nvir = dvv.shape[0]
        print('nocc', nocc)
        print('nvir', nvir)
        dov  = np.zeros((nocc,nvir), dtype=doo.dtype)
        dvo  = dov.T
        return ccsd_rdm._make_rdm1(mp,(doo,dov,dvo,dvv),with_frozen=False)

    def _gamma1_intermediates(mp,t2=None):
        nmo  = mp.nmo
        nocc = mp.nocc
        nvir = nmo - nocc
        from pyscf.mp import mp2
        mo_coeff  = mp2._mo_without_core(mp, mp.mo_coeff)
        print("mo coeff rdms", mo_coeff)
        print('nmo, nocc, nvir, mo_coeff shape')
        print(nmo, nocc, nvir, mp.mo_coeff.shape)
        mo_energy = mp2._mo_energy_without_core(mp, mp.mo_energy)
        eia = mo_energy[:nocc,None] - mo_energy[None,nocc:]
        if(t2 is None):
            for istep, qov in enumerate(mp.loop_ao2mo(mo_coeff, nocc)):
                if(istep==0):
                    dtype = qov.dtype
                    dm1occ = np.zeros((nocc,nocc), dtype=dtype)
                    dm1vir = np.zeros((nvir,nvir), dtype=dtype)
                for i in range(nocc):
                    buf = np.dot(qov[:,i*nvir:(i+1)*nvir].T,
                                   qov).reshape(nvir,nocc,nvir)
                    gi = np.array(buf, copy=False)
                    gi = gi.reshape(nvir,nocc,nvir).transpose(1,0,2)
                    t2i = gi/lib.direct_sum('jb+a->jba', eia, eia[i])
                    l2i = t2i.conj()
                    dm1vir += np.einsum('jca,jcb->ba', l2i, t2i) * 2 \
                           - np.einsum('jca,jbc->ba', l2i, t2i)
                    dm1occ += np.einsum('iab,jab->ij', l2i, t2i) * 2 \
                           - np.einsum('iab,jba->ij', l2i, t2i)
        else:
            dtype = t2[0].dtype
            dm1occ = np.zeros((nocc,nocc), dtype=dtype)
            dm1vir = np.zeros((nvir,nvir), dtype=dtype)
            for i in range(nocc):
                t2i = t2[i]
                l2i = t2i.conj()
                dm1vir += np.einsum('jca,jcb->ba', l2i, t2i) * 2 \
                      - np.einsum('jca,jbc->ba', l2i, t2i)
                dm1occ += np.einsum('iab,jab->ij', l2i, t2i) * 2 \
                      - np.einsum('iab,jba->ij', l2i, t2i)
        return -dm1occ, dm1vir

    def make_rdm2(mp2solver):
        nmo  = nmo0  = mp2solver.nmo
        nocc = nocc0 = mp2solver.nocc
        nvir = nmo - nocc
        from pyscf.mp import mp2
        mo_coeff  = mp2._mo_without_core(mp2solver, mp2solver.mo_coeff)
        mo_energy = mp2._mo_energy_without_core(mp2solver, mp2solver.mo_energy)
        eia       = mo_energy[:nocc,None] - mo_energy[None,nocc:]

        moidx = oidx = vidx = None
        dm1   = make_rdm1(mp2solver)
        dm1[np.diag_indices(nocc0)] -= 2
        dm2   = np.zeros((nmo0,nmo0,nmo0,nmo0), dtype=dm1.dtype)

        if(t2 is None):
            for istep, qov in enumerate(mp2solver.loop_ao2mo(mo_coeff, nocc)):
                for i in range(nocc):
                    buf = np.dot(qov[:,i*nvir:(i+1)*nvir].T,qov).reshape(nvir,nocc,nvir)
                    gi  = np.array(buf,copy=False)
                    gi  = gi.reshape(nvir,nocc,nvir).transpose(1,0,2)
                    t2i = gi.conj()/lib.direct_sum('jb+a->jba',eia,eia[i])
                    dovov = t2i.transpose(1,0,2)*2 - t2i.transpose(2,0,1)
                    dovov *= 2
                    if moidx is None:
                        dm2[i,nocc:,:nocc,nocc:] = dovov
                        dm2[nocc:,i,nocc:,:nocc] = dovov.conj().transpose(0,2,1)
                    else:
                        dm2[oidx[i],vidx[:,None,None],oidx[:,None],vidx] = dovov
                        dm2[vidx[:,None,None],oidx[i],vidx[:,None],oidx] = dovov.conj().transpose(0,2,1)

        else:
            for i in range(nocc):
                t2i = t2[i]
                dovov = t2i.transpose(1,0,2)*2 - t2i.transpose(2,0,1)
                dovov *= 2
                if moidx is None:
                    dm2[i,nocc:,:nocc,nocc:] = dovov
                    dm2[nocc:,i,nocc:,:nocc] = dovov.conj().transpose(0,2,1)
                else:
                    dm2[oidx[i],vidx[:,None,None],oidx[:,None],vidx] = dovov
                    dm2[vidx[:,None,None],oidx[i],vidx[:,None],oidx] = dovov.conj().transpose(0,2,1)

        for i in range(nocc0):
            dm2[i,i,:,:] += dm1.T * 2
            dm2[:,:,i,i] += dm1.T * 2
            dm2[:,i,i,:] -= dm1.T
            dm2[i,:,:,i] -= dm1

        for i in range(nocc0):
            for j in range(nocc0):
                dm2[i,i,j,j] += 4
                dm2[i,j,j,i] -= 2

        return dm2


    t2 = None
    rdm1 = make_rdm1(mp2solver)
    rdm2 = make_rdm2(mp2solver)

#  # -------------------------------------
 # TEST: compare rdm dmet with dfmp2 rdms
    # atoms_test=\
    # [['C',( 0.0000, 0.0000, 0.7680)],\
    #  ['C',( 0.0000, 0.0000,-0.7680)],\
    #  ['H',(-1.0192, 0.0000, 1.1573)],\
    #  ['H',( 0.5096, 0.8826, 1.1573)],\
    #  ['H',( 0.5096,-0.8826, 1.1573)],\
    #  ['H',( 1.0192, 0.0000,-1.1573)],\
    #  ['H',(-0.5096,-0.8826,-1.1573)],\
    #  ['H',(-0.5096, 0.8826,-1.1573)]]

    atoms_test = [
    ['O' , (0. , 0. , 0.)],\
    ['H' , (0. , -0.757 , 0.587)],\
    ['H' , (0. , 0.757  , 0.587)]]

    mol_test = gto.M(atom=atoms_test,basis='cc-pvdz')
    m_test = scf.RHF(mol_test).density_fit()
    m_test.kernel()

    mm_test = dfmp2.DFMP2(m_test)
    mm_test.kernel()
    from pyscf.mp import mp2
    rdm1_test = mp2.make_rdm1(mm_test)
    rdm2_test = mp2.make_rdm2(mm_test)

    # Plot sorted rdm1 values
    x1 = rdm1
    y1 = x1.flatten()
    y1 = np.sort(y1)
    import matplotlib.pyplot as plt
    plt.plot(y1, 'r', label='rdm1 from dmet')
    plt.ylabel('rdm1')
    x2 = rdm1_test
    y2 = x2.flatten()
    print("rdm1", y2)
    print(type(y2))
    y2 = np.sort(y2)
    plt.plot(y2, 'b', label='rdm1 for dfmp2')
    plt.ylabel('rdm1 sorted values')
    # Place a legend above this subplot, expanding itself to
    # fully use the given bounding box.
    plt.legend(bbox_to_anchor=(0., 1.02, 1., .102), loc=3,
               ncol=2, mode="expand", borderaxespad=0.)
    plt.show()
    plt.close()
    print("deviations between sorted 1rdm in MO basis ")
    print(np.abs(y1-y2).max())

    # Plot sorted rdm2 values
    x1 = rdm2
    y1 = x1.flatten()
    y1 = np.sort(y1)
    import matplotlib.pyplot as plt
    plt.plot(y1, 'r', label='rdm2 from dmet')
    plt.ylabel('rdm2')
    x2 = rdm2_test
    y2 = x2.flatten()
    print("rdm1", y2)
    print(type(y2))
    y2 = np.sort(y2)
    plt.plot(y2, 'b', label='rdm2 for dfmp2')
    plt.ylabel('rdm1 sorted values')
    # Place a legend above this subplot, expanding itself to
    # fully use the given bounding box.
    plt.legend(bbox_to_anchor=(0., 1.02, 1., .102), loc=3,
               ncol=2, mode="expand", borderaxespad=0.)
    plt.show()
    plt.close()
    print("deviations between sorted 1rdm in MO basis ")
    print(np.abs(y1-y2).max())

    print("deviations between 1rdm,2rdm in MO basis ")
    print(np.abs(rdm1-rdm1_test).max())
    print(np.abs(rdm2-rdm2_test).max())
# # ----------------------------------

    # transform rdm's to original basis
    tei  = ao2mo.restore(1, intsp_df, cfx.shape[1])
    print("tei shape", tei.shape)
    rdm1 = np.dot(cf, np.dot(rdm1, cf.T))
    rdm2 = np.einsum('ai,ijkl->ajkl', cf, rdm2)
    rdm2 = np.einsum('bj,ajkl->abkl', cf, rdm2)
    rdm2 = np.einsum('ck,abkl->abcl', cf, rdm2)
    rdm2 = np.einsum('dl,abcl->abcd', cf, rdm2)

    ImpEnergy = +0.25 *np.einsum('ij,jk,ki->', 2*Hp+jkp, rdm1, Xp) \
                +0.25 *np.einsum('ij,jk,ki->', 2*Hp+jkp, Xp, rdm1) \
                +0.125*np.einsum('ijkl,ijkm,ml->', tei, rdm2, Xp) \
                +0.125*np.einsum('ijkl,ijml,mk->', tei, rdm2, Xp) \
                +0.125*np.einsum('ijkl,imkl,mj->', tei, rdm2, Xp) \
                +0.125*np.einsum('ijkl,mjkl,mi->', tei, rdm2, Xp)

    Nel = np.trace(np.dot(np.dot(rdm1, Sp), Xp))

    return Nel, ImpEnergy
