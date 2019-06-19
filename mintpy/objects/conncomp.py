############################################################
# Program is part of MintPy                                #
# Copyright(c) 2018-2019, Zhang Yunjun                     #
# Author:  Zhang Yunjun                                    #
############################################################
# Recommend import:
#   from mintpy.objects.conncomp import connectComponent


import os
import time
import itertools
import numpy as np
from scipy.sparse import csgraph as csg
from scipy.spatial import cKDTree
try:
    from skimage import measure, segmentation as seg, morphology as morph
except ImportError:
    raise ImportError('Could not import skimage!')
from .ramp import deramp


class connectComponent:
    """ Object for bridging connected components.
    
    Example:
        unw_file = 'filt_fine.unw'
        # prepare connectComponent object
        atr = readfile.read_attribute(unw_file)
        conncomp = readfile.read(unw_file+'.conncomp')[0]
        cc = connectComponent(conncomp=conncomp, metadata=atr)
        cc.label()
        cc.find_mst_bridge()
        # run bridging
        unw = readfile.read(unw_file)[0]
        bdg_unw = cc.unwrap_conn_comp(unw, ramp_type='linear')
        # write output file
        writefile.write(bdg_unw, 'bdg_'+unw_file, atr)
    """
    def __init__(self, conncomp, metadata):
        """Parameters: conncomp : 2D np.ndarray in np.bool_ format
                       metadata : dict, attributes
        """
        if type(conncomp).__module__ != np.__name__:
            raise ValueError('Input conncomp is not np.ndarray: {}'.format(type(conncomp).__module__))
        self.conncomp = conncomp
        self.metadata = metadata
        if 'REF_Y' in metadata.keys():
            self.refY = int(self.metadata['REF_Y'])
            self.refX = int(self.metadata['REF_X'])
        else:
            self.refY = None
            self.refX = None
        self.length, self.width = self.conncomp.shape

    def label(self, min_area=2.5e3, erosion_size=5, print_msg=False):
        """ Label the connected components
        Returns: self.labelImg   - 2D np.ndarray in int64 to mask areas to be corrected
                 self.labelBound - 2D np.ndarray in uint8 for label boundaries to find bridges
        """
        # 1. labelImg
        (self.labelImg, 
         self.numLabel, 
         self.labelBound) = self.get_large_label(self.conncomp,
                                                 min_area=min_area,
                                                 erosion_size=erosion_size,
                                                 get_boundary=True,
                                                 print_msg=print_msg)

        # 2. reference label (ref_y/x or the largest one)
        if self.refY is not None:
            self.labelRef = self.labelImg[self.refY, self.refX]
        else:
            regions = measure.regionprops(self.labelImg)
            idx = np.argmax([region.area for region in regions])
            self.labelRef = regions[idx].label
        return

    @staticmethod
    def get_large_label(mask, min_area=2.5e3, erosion_size=5, get_boundary=False, print_msg=False):
        # initial label
        label_img, num_label = measure.label(mask, connectivity=1, return_num=True)

        # remove regions with small area
        if print_msg:
            print('remove regions with area < {}'.format(int(min_area)))
        min_area = min(min_area, label_img.size * 3e-3)
        flag_slabel = np.bincount(label_img.flatten()) < min_area
        flag_slabel[0] = False
        label_small = np.where(flag_slabel)[0]
        for i in label_small:
            label_img[label_img == i] = 0
        label_img, num_label = measure.label(label_img, connectivity=1, return_num=True) # re-label

        # remove regions that would disappear after erosion operation
        erosion_structure = np.ones((erosion_size, erosion_size))
        label_erosion_img = morph.erosion(label_img, erosion_structure).astype(np.uint8)
        erosion_regions = measure.regionprops(label_erosion_img)
        if len(erosion_regions) < num_label:
            if print_msg:
                print('Some regions are lost during morphological erosion operation')
            label_erosion = [reg.label for reg in erosion_regions]
            for orig_reg in measure.regionprops(label_img):
                if orig_reg.label not in label_erosion:
                    if print_msg:
                        print('label: {}, area: {}, bbox: {}'.format(orig_reg.label, 
                                                                     orig_reg.area,
                                                                     orig_reg.bbox))
                    label_img[label_img == orig_reg.label] = 0
        label_img, num_label = measure.label(label_img, connectivity=1, return_num=True) # re-label

        # get label boundaries to facilitate bridge finding
        if get_boundary:
            label_bound = seg.find_boundaries(label_erosion_img, mode='thick').astype(np.uint8)
            label_bound *= label_erosion_img
            return label_img, num_label, label_bound
        else:
            return label_img, num_label

    def get_all_bridge(self):
        regions = measure.regionprops(self.labelBound)

        trees = []
        for i in range(self.numLabel):
            trees.append(cKDTree(regions[i].coords))

        self.connDict = dict()
        self.distMat = np.zeros((self.numLabel, self.numLabel), dtype=np.float32)
        for i, j in itertools.combinations(range(self.numLabel), 2):
            # find shortest bridge
            dist, idx = trees[i].query(regions[j].coords)
            idx_min = np.argmin(dist)
            yxj = regions[j].coords[idx_min,:]
            yxi = regions[i].coords[idx[idx_min],:]
            dist_min = dist[idx_min]
            # save
            n0, n1 = str(i+1), str(j+1)
            conn = dict()
            conn[n0] = yxi
            conn[n1] = yxj
            conn['distance'] = dist_min
            self.connDict['{}{}'.format(n0, n1)] = conn
            self.distMat[i,j] = self.distMat[j,i] = dist_min
        return

    def find_mst_bridge(self):
        if not hasattr(self, 'distMat'):
            self.get_all_bridge()

        # MST bridges with breadth_first_order
        distMatMst = csg.minimum_spanning_tree(self.distMat)
        succs, preds = csg.breadth_first_order(distMatMst, i_start=self.labelRef-1, directed=False)

        # save to self.bridges
        self.bridges = []
        for i in range(1, succs.size):
            n0 = preds[succs[i]] + 1
            n1 = succs[i] + 1
            # read conn
            nn = sorted([str(n0), str(n1)])
            conn = self.connDict['{}{}'.format(nn[0], nn[1])]
            y0, x0 = conn[str(n0)]
            y1, x1 = conn[str(n1)]
            # save bdg
            bridge = dict()
            bridge['x0'] = x0
            bridge['y0'] = y0
            bridge['x1'] = x1
            bridge['y1'] = y1
            bridge['label0'] = n0
            bridge['label1'] = n1
            self.bridges.append(bridge)
        self.num_bridge = len(self.bridges)
        return

    def get_bridge_endpoint_aoi_mask(self, bridge, radius=50):
        # get AOI mask
        x0, y0 = bridge['x0'], bridge['y0']
        x1, y1 = bridge['x1'], bridge['y1']
        x00 = max(0, x0 - radius); x01 = min(self.width,  x0 + radius)
        y00 = max(0, y0 - radius); y01 = min(self.length, y0 + radius)
        x10 = max(0, x1 - radius); x11 = min(self.width,  x1 + radius)
        y10 = max(0, y1 - radius); y11 = min(self.length, y1 + radius)
        aoi_mask0 = np.zeros(self.labelImg.shape, dtype=np.bool_)
        aoi_mask1 = np.zeros(self.labelImg.shape, dtype=np.bool_)
        aoi_mask0[y00:y01, x00:x01] = True
        aoi_mask1[y10:y11, x10:x11] = True
        return aoi_mask0, aoi_mask1

    def unwrap_conn_comp(self, unw, radius=50, ramp_type=None, print_msg=False):
        start_time = time.time()
        radius = int(min(radius, min(self.conncomp.shape)*0.05))

        unw = np.array(unw, dtype=np.float32)
        if self.refY is not None:
            unw[unw != 0.] -= unw[self.refY, self.refX]

        if ramp_type is not None:
            if print_msg:
                print('estimate a {} ramp'.format(ramp_type))
            ramp_mask = (self.labelImg == self.labelRef)
            unw, ramp = deramp(unw, ramp_mask, ramp_type, metadata=self.metadata)

        for bridge in self.bridges:
            # prepare masks
            aoi_mask0, aoi_mask1 = self.get_bridge_endpoint_aoi_mask(bridge, radius=radius)
            label_mask0 = self.labelImg == bridge['label0']
            label_mask1 = self.labelImg == bridge['label1']

            # get phase difference
            value0 = np.nanmedian(unw[aoi_mask0 * label_mask0])
            value1 = np.nanmedian(unw[aoi_mask1 * label_mask1])
            diff_value = value1 - value0

            # estimate integer number of phase jump
            num_jump = (np.abs(diff_value) + np.pi) // (2.*np.pi)
            if diff_value > 0:
                num_jump *= -1

            # add phase jump
            unw[label_mask1] += 2.* np.pi * num_jump

            if print_msg:
                print(('phase diff {}-{}: {:04.1f} rad --> '
                       'num of jump: {}').format(bridge['label1'],
                                                 bridge['label0'],
                                                 diff_value,
                                                 num_jump))

        # add ramp back
        if ramp_type is not None:
            unw += ramp
        if print_msg:
            print('time used: {:.2f} secs.'.format(time.time()-start_time))
        return unw

    def plot_bridge(self, ax, cmap='jet', radius=50):
        # label background
        im = ax.imshow(self.labelImg, cmap=cmap)
        # bridges
        for bridge in self.bridges:
            ax.plot([bridge['x0'], bridge['x1']],
                    [bridge['y0'], bridge['y1']], 'w-', lw=1)
            # endpoint window
            if radius > 0:
                aoi_mask0, aoi_mask1 = self.get_bridge_endpoint_aoi_mask(bridge, radius=radius)
                label_mask0 = self.labelImg == bridge['label0']
                label_mask1 = self.labelImg == bridge['label1']
                mask0 = np.ma.masked_where(~(aoi_mask0*label_mask0), np.zeros(self.labelImg.shape))
                mask1 = np.ma.masked_where(~(aoi_mask1*label_mask1), np.zeros(self.labelImg.shape))
                ax.imshow(mask0, cmap='gray', alpha=0.3, vmin=0, vmax=1)
                ax.imshow(mask1, cmap='gray', alpha=0.3, vmin=0, vmax=1)
        # reference pixel
        ax.plot(self.refX, self.refY, 'ks', ms=2)        
        return ax


