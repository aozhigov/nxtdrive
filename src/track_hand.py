#!/usr/bin/python

import roslib
roslib.load_manifest('cv_nxtdrive')
import rospy
from sensor_msgs.msg import Image
from geometry_msgs.msg import Point
from cv_nxtdrive.msg import HandRect
from cv_bridge import CvBridge, CvBridgeError
import sys
sys.path = ['/home/alex/opencv_lib/lib/python2.7/dist-packages'] + sys.path
import cv2
from cv2.cv import RGB, CV_FOURCC
import numpy as np
import os
from os import errno
from os.path import basename
from sys import exit
from sys import argv
from sys import stderr
import matplotlib as mpl
import matplotlib.pyplot as plt
from time import sleep
from math import sqrt
import pickle
from histogram import Histogram
from myutils import calc_back_proj, draw_debug_messages, draw_cross
from common_utils import prepare_env

from kcurv import kcurv

NXT_CTL_TOPIC = '/nxt_motor_ctl'
UNKNOWN_ANGLE = 400
CTL_TURN_VALUE_MIN = 0.0

depth_hist_file = 'depth_histogram.txt'
DEF_TOPIC_NAME = '/camera/depth_registered/image_rect'
bridge = CvBridge()

import cProfile
import pickle

class Enum(set):
    def __getattr__(self, name):
        if name in self:
            return name
        raise AttributeError

ThresholdMode = Enum(['DEPTH_AUTO', 'DEPTH_MANUAL'])
TrackerState = Enum(['INIT', 'RUN'])

def dist(p1, p2):
    d1 = p1[0] - p2[0]
    d2 = p1[1] - p2[1]
    return sqrt(d1 * d1 + d2 * d2)

def tri_square(p1, p2, p3):
    a = dist(p1, p2)
    b = dist(p2, p3)
    c = dist(p1, p3)
    p = (a + b + c) / 2.0
    try:
        s = np.sqrt(p * (p - a) * (p - b) * (p - c))
    except:
        print 'ERROR IN tri_square!!!'
    return s

def build_line(p1, p2, debug_img = False):
    ret_line = np.empty([0, 1, 2], dtype='int32')
    p_cur = [p1[0], p1[1]]
    x_delta = 1 if p1[0] < p2[0] else -1
    y_delta = 1 if p1[1] < p2[1] else -1
    delta = (x_delta, y_delta)
    w = abs(p1[0] - p2[0])
    h = abs(p1[1] - p2[1])
    acc_delta = (w, h)
    acc = [w, h]
    cur_acc, other_acc = (1, 0) if acc[0] > acc[1] else (0, 1)
    i = 0
    while np.any(p_cur != p2):
        ret_line = np.append(ret_line, np.array([[[p_cur[0], p_cur[1]]]], dtype='int32'), axis=0)
        if acc[cur_acc] == acc[other_acc]:
            p_cur[cur_acc] += delta[cur_acc]
            p_cur[other_acc] += delta[other_acc]
            acc[cur_acc] += acc_delta[cur_acc]
            cur_acc, other_acc = other_acc, cur_acc
        elif acc[cur_acc] < acc[other_acc]:
            p_cur[other_acc] += delta[other_acc]
        elif acc[cur_acc] > acc[other_acc]:
            p_cur[cur_acc] += delta[cur_acc]
            cur_acc, other_acc = other_acc, cur_acc
        acc[cur_acc] += acc_delta[cur_acc]

    if debug_img:
        img = np.zeros([max(p1[1], p2[1]) + 1, max(p1[0], p2[0]) + 1], dtype='uint8')
        for p in ret_line:
            _p = p[0]
            img[_p[1]][_p[0]] = 255

    else:
        img = None
    return (ret_line, img)


def my_find_contours(img):
    visited = np.ones(img.shape, dtype='bool')


def max_filter(img):
    s = img.shape
    img_f = np.zeros((s[0] + 2, s[1] + 2), dtype='uint8')
    img_ret = np.zeros((s[0], s[1]), dtype='uint8')
    img_f[1:s[0] + 1, 1:s[1] + 1] = img
    for i in xrange(1, s[0]):
        for j in xrange(1, s[1]):
            img_ret[i][j] = np.max(img_f[i - 1:i + 2, j - 1:j + 2])

    return img_ret

def test_nxt_ctl_pub(pub):
    for i in range(10):
        pub.publish(Point(i, i, i))

class HandTracker():
    MIN_DIST = 3.0

    def __init__(self, topic = DEF_TOPIC_NAME, mode = ThresholdMode.DEPTH_AUTO, use_recognition = False):
        self.result_file = file('result.txt', 'w')
        self.vwriter_result = None
        self.vwriter_rgb = None
        self.kcurv_stat_cnt = 0
        self.kcurv_stat_cnt_max = 100
        self.kcurv_stat_vals = np.array([])
        self.kcurv_stat_str = ''
        self.mouse_x = 0
        self.mouse_y = 0
        self.state = TrackerState.INIT
        p1 = Point(0, 0, 0)
        p2 = Point(640, 480, 0)
        self.hand_area = (p1, p2)
        self.recog_cnt = 0
        self.norecog_cnt_min = 30
        self.norecog_cnt = 0
        self.recog_cnt_min = 5
        self.init_state_max = 5
        self.state_cnt = 0
        self.bbox_inited = False
        self.bbox_init = None
        self.turn_angle_init = 0
        self.turn_angle = self.turn_angle_init
        self.img_fore = None
        self.MAX_RANGE = 5.0
        self.write_dist_hist = False
        self.dist_hist_sample_size = 100
        self.dist_hist_sample_cnt = 0
        self.dist_hist_sample_number = 0
        self.stat_pixel_pos = (175, 282)
        self.dist_hist_fname_base = 'dist_hist'
        self.dist_pixel_fname = 'dist_pixel.txt'
        self.dist_hist_imgs = np.zeros([self.dist_hist_sample_size, 480, 640])
        self.img_filter_buf = self.MAX_RANGE * np.ones([480, 640])
        self.new_img_filter_buf = self.MAX_RANGE * np.ones([480, 640])
        self.dist_hist_imgs_i = 0
        self.debug_cnt = 0
        self.mode = mode
        self.depth_frame_cnt = 0
        rospy.init_node('track_hand', anonymous=True)
        #cv2.namedWindow('wnd_orig', 0)
        cv2.namedWindow('wnd_prob', 0)
        cv2.namedWindow('wnd_contours', 0)
        #cv2.setMouseCallback('wnd_orig', self.on_orig_mouse, None)
        self.depth_threshold = 127
        if self.mode == ThresholdMode.DEPTH_MANUAL:
            cv2.createTrackbar('track_depth', 'wnd_prob', self.depth_threshold, 255, self.on_depth_track)
        rospy.Subscriber(topic, Image, self.depth_cb)
        rospy.Subscriber('/camera/rgb/image_rect_color', Image, self.rgb_cb)
        if use_recognition:
            self.use_recognition = True
            rospy.Subscriber('/hand_recognizer/pos', HandRect, self.hand_rect_cb)
        else:
            self.use_recognition = False

        self.nxt_ctl_pub = rospy.Publisher(NXT_CTL_TOPIC, Point)
        test_nxt_ctl_pub(self.nxt_ctl_pub)

    def set_result_video(self, fname):
        self.vwriter_result = cv2.VideoWriter(fname, \
                               CV_FOURCC('P', 'I', 'M', '1'), \
                               30.0, (640, 480), True)

    def set_rgb_video(self, fname):
        self.vwriter_rgb = cv2.VideoWriter(fname, \
                               CV_FOURCC('P', 'I', 'M', '1'), \
                               30.0, (640, 480), True)

    def on_orig_mouse(self, event, x, y, i, data):
        self.mouse_x = x
        self.mouse_y = y

    def on_depth_track(self, pos, *argv):
        self.depth_threshold = pos

    def get_auto_threshold(self, img_roi):
        min_dist_m = np.min(img_roi)
        #print 'MIN_DIST', min_dist_m
        return min_dist_m

    def get_foreground_uint8(self, img):
        #first get threshold value and do depth tresholding
        RANGE_DELTA_M = 0.15
        if self.mode == ThresholdMode.DEPTH_MANUAL:
            d_thr = self.depth_threshold / 256.0 + 0.5
        elif self.mode == ThresholdMode.DEPTH_AUTO:
            p1 = self.hand_area[0]
            p2 = self.hand_area[1]
            if self.recog_cnt < self.recog_cnt_min:#unstable recognition
                img_roi = img
            else:
                img_roi = img[p1.y:p2.y, p1.x:p2.x]
            d_thr = self.get_auto_threshold(img_roi)
            #second filter out irrelevant objects (nothing if hand recognition is off)
            img_croped = self.MAX_RANGE * np.ones(img.shape, img.dtype)
            if self.recog_cnt >= self.recog_cnt_min:#stable recognition, crop foreground
                img_croped[p1.y:p2.y, p1.x:p2.x] = img_roi
                img = img_croped
        depth_bin = cv2.threshold(img, d_thr + RANGE_DELTA_M, 255, cv2.THRESH_BINARY_INV)
        return depth_bin[1].astype('uint8')

    @classmethod
    def draw_contours_info(cls, cont, img_contours, merge_points):
        for i in range(len(cont[0])):
            c = cont[0][i]
            brect = cv2.boundingRect(c)
            org = (brect[0] + brect[2] / 2, brect[1] + brect[3] / 2)
            fnt = cv2.FONT_HERSHEY_PLAIN
            clr = RGB(0, 0, 255)
            cv2.putText(img_contours, str(i), org, fnt, 1.0, clr)

        if merge_points != None:
            for p in merge_points:
                pass
                #cv2.circle(img_contours, tuple(p), 10, clr)

    @classmethod
    def dump_contours(cls, cont, fname):
        d = {}
        for i in range(len(cont)):
            li = len(cont[i])
            for j in [ x for x in range(len(cont)) if x != i ]:
                lj = len(cont[j])
                d[str(i) + 'B ' + str(j) + 'B'] = dist(cont[i][0][0], cont[j][0][0])
                d[str(i) + 'B ' + str(j) + 'E'] = dist(cont[i][0][0], cont[j][lj - 1][0])
                d[str(i) + 'E ' + str(j) + 'B'] = dist(cont[i][li - 1][0], cont[j][0][0])
                d[str(i) + 'E ' + str(j) + 'E'] = dist(cont[i][li - 1][0], cont[j][lj - 1][0])

        pickle.dump(d, file(fname, 'w'))

    @classmethod
    def check_contour(cls, c, ci):
        for j in range(len(c) - 1):
            p1 = c[j]
            p2 = c[j + 1]
            d = dist(p1[0], p2[0])
            if d > HandTracker.MIN_DIST:
                raise Exception('BOOM in i%d j%d d%d %d %d %d %d' % (j,
                 j + 1,
                 d,
                 p1[0][0],
                 p1[0][1],
                 p2[0][0],
                 p2[0][1]))

        for i in range(len(c) - 1):
            for j in range(len(c) - 1):
                if j == i:
                    continue
                if np.all(c[i] == c[j]):
                    raise Exception('Equal contour elements(%d): %d and %d' % (ci, i, j))

    @classmethod
    def check_contours(cls, cont):
        for i in range(len(cont)):
            c = cont[i]

    @classmethod
    def prune_contours(cls, cont):
        for i in range(len(cont)):
            c = cont[i]
            new_c = np.array(c[0], dtype='int32')
            for j in range(len(c) - 1):
                for k in range(j + 1, len(c)):
                    pass

    def get_contours(self, img_fore):
        #img_ed_canny = cv2.Canny(img_fore, 10, 40)
        #img_ed = img_ed_canny
        img_ed = img_fore.copy()
        #img_ed = max_filter(img_ed_canny)
        cont = cv2.findContours(img_ed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        #pickle.dump(cont[0], file('c0', 'w'))
        cont = ([ c for c in cont[0] if len(c.shape) == 3 and len(c) > 2 ], cont[1])
        img_contours = np.zeros([480, 640, 3], dtype=np.uint8)
        cv2.drawContours(img_contours, cont[0], -1, RGB(255, 0, 0))
        fname = 'conts/%03.dat' % self.depth_frame_cnt
        #pickle.dump(cont[0], file(fname, 'w'))
        return (cont[0], img_contours)

    @classmethod
    def find_close_conts(cls, cont):
        for i in range(len(cont)):
            c_i0 = cont[i][0]
            c_il = cont[i][len(cont[i]) - 1]
            for j in [ x for x in range(len(cont)) if x != i ]:
                c_j0 = cont[j][0]
                c_jl = cont[j][len(cont[j]) - 1]
                if dist(c_i0[0], c_j0[0]) < HandTracker.MIN_DIST:
                    return (i,
                     j,
                     0,
                     0)
                if dist(c_i0[0], c_jl[0]) < HandTracker.MIN_DIST:
                    return (i,
                     j,
                     0,
                     len(cont[j]) - 1)
                if dist(c_il[0], c_jl[0]) < HandTracker.MIN_DIST:
                    return (i,
                     j,
                     len(cont[i]) - 1,
                     len(cont[j]) - 1)
                if dist(c_il[0], c_j0[0]) < HandTracker.MIN_DIST:
                    return (i,
                     j,
                     len(cont[i]) - 1,
                     0)

        return (-1, -1, -1, -1)

    @classmethod
    def merge_two_contours(cls, cont, i, j, i_pos, j_pos):
        a = cont[i] if i_pos != 0 else cont[i][::-1]
        b = cont[j] if j_pos == 0 else cont[j][::-1]
        middle, _ = build_line(cont[i][i_pos][0], cont[j][j_pos][0])
        ret = np.append(a, middle, axis=0)
        ret = np.append(ret, b, axis=0)
        return ret

    @classmethod
    def merge_contours(cls, cont, merge_points):
        try:
            cls.merge_i += 1
        except:
            cls.merge_i = 0

        cls.dump_contours(cont, 'd%03d' % cls.merge_i)
        i, j, i_pos, j_pos = cls.find_close_conts(cont)
        if i == -1:
            return (cont, merge_points)
        new_cont = [ cont[k] for k in range(len(cont)) if k != i and k != j ]
        #print 'Merging %d %d' % (i, j)
        merged_pair = cls.merge_two_contours(cont, i, j, i_pos, j_pos)
        merge_points = np.append(merge_points, [cont[i][i_pos][0], cont[j][j_pos][0]], axis=0)
        new_cont.append(merged_pair)
        return cls.merge_contours(new_cont, merge_points)

    def plot_dist_hist(self, imgs_float, fname):
        v = np.var(imgs_float, axis=0)
        v_min = np.min(v)
        v_max = np.max(v)
        cv2.imwrite('depth_var.png', (v * 255.0 / v_max).astype('uint8'))
        large_v = np.where(v > 0.5)
        v_step = (v_max - v_min) / 3
        cmap = mpl.colors.ListedColormap(['black', 'blue', 'red'])
        bounds = [v_min,
         v_min + v_step,
         v_min + v_step * 2,
         v_max]
        norm = mpl.colors.BoundaryNorm(bounds, cmap.N)
        plt.clf()
        img = plt.imshow(v, interpolation='nearest', cmap=cmap, norm=norm)
        plt.colorbar(img, cmap=cmap, norm=norm, boundaries=bounds, ticks=[v_min, 0, v_max])
        plt.savefig(fname)

    def save_stat_pixel_data(self, imgs):
        f = file(self.dist_pixel_fname, 'w')
        u, v = self.stat_pixel_pos
        for i in range(len(imgs)):
            f.write(str(imgs[i][v][u]) + '\n')

        f.close()

    def write_dist_hist_img(self, img_float):
        if self.dist_hist_imgs_i == self.dist_hist_sample_size:
            self.save_stat_pixel_data(self.dist_hist_imgs)
            fname = self.dist_hist_fname_base + '_%02d' % self.dist_hist_sample_number
            self.dist_hist_sample_number += 1
            self.plot_dist_hist(self.dist_hist_imgs, fname)
            self.dist_hist_imgs = np.zeros([self.dist_hist_sample_size, 480, 640])
            self.dist_hist_imgs_i = 0
        else:
            self.dist_hist_imgs[self.dist_hist_imgs_i] = img_float
            self.dist_hist_imgs_i += 1

    @classmethod
    def get_kcurvs(cls, contours, delta):
        kcurvs = []
        conts = []
        kcurv_max_i = -1
        kcurv_max_len = -1
        for i in range(len(contours)):
            c = contours[i]
            if len(c) > kcurv_max_len:
                kcurv_max_len = len(c)
                kcurv_max_i = i
        '''
        for c in contours:
            kc, cont = kcurv(c, delta)
            kcurvs.append(kc)
            conts.append(cont)
        '''
        if kcurv_max_i > -1:
            kc, cont = kcurv(contours[kcurv_max_i], delta)
            kcurvs.append(kc)
            conts.append(cont)
        return (kcurvs, conts)

    @classmethod
    def sort_kcurv_fingers(cls, kcurvs, finger_tips, bbox_c):
        ftips_sorted = np.zeros(finger_tips.shape, finger_tips.dtype)

    @classmethod
    def get_kcurv_fingers(cls, kcurvs, bbox):
        KCURV_MAX_ANGLE = 1.32
        fingers = []
        finger_tips = []
        middle_finger_pos = (0, 0)
        middle_finger_height = 480
        bbox_cx = bbox[1] + bbox[3] / 2
        bbox_cy = bbox[0] + bbox[2] / 2
        for i in range(len(kcurvs)):
            kc = kcurvs[i]
            fing = np.array([i for i in range(len(kc)) if kc[i][1] < KCURV_MAX_ANGLE])
            fingers.append(fing)
            ftips = np.array([i for i in range(len(kc)) if kc[i][1] < KCURV_MAX_ANGLE and dist((bbox_cx, bbox_cy), kc[i][0]) > bbox[2] / 3])
            #ftips_sorted = self.sort_kcurv_fingers(kc, ftips, (bbox_cx, bbox_cy))
            finger_tips.append(ftips)
        #Now find the thumb and little finger. Thumb and little finger tips have maximum distance between them among all pairs of finger tips
        thumb_n_point = []
        for i in range(len(kcurvs)):
            kc = kcurvs[i]
            ftips = finger_tips[i]
            max_dist = 1
            tnp = np.array([0, 1])
            for k in range(len(ftips)):
                for l in range(len(ftips)):
                    if k == l:
                        continue
                    d = dist(kc[k][0], kc[l][0])
                    if d > max_dist:
                        max_dist = d
                        tnp = np.array([k, l])
            thumb_n_point.append(tnp)
        return (fingers, finger_tips, thumb_n_point, middle_finger_pos)

    def recognize_hand_kcurv(self, kcurvs):
        for i in range(len(kcurvs)):
            kc = kcurvs[i]
            if len(kc) >= 6 and len(kc) < 15:
                return i
        return -1

    def get_max_hull_defect_ind(self, defects, cont, bbox):
        dmax_dist = 0
        dmax_i = 0
        smax = 0
        smax_i = 0
        #print '______________'
        print '%d. ' % self.depth_frame_cnt, 
        for i in range(len(defects)):
            p1 = cont[defects[i][0][0]][0]
            p2 = cont[defects[i][0][1]][0]
            p3 = cont[defects[i][0][2]][0]
            s = tri_square(p1, p2, p3)
            dp = defects[i][0][3]
            d = dist(p1, p2)
            if s > smax and d > 0.4 * bbox[2]:
                smax = s
                smax_i = i
            print 'S %.3f ' % s,
            if d > 0.6 * bbox[2] and dp / 256.0 > 0.28 * bbox[3]:
                dmax_dist = d
                dmax_i = i
        print '\nChosen %.3f' % smax
        return smax_i
        #return dmax_i
        
    def calc_angle(self, defc_max, c, bbox):
        cx = bbox[1] + bbox[3] / 2
        cy = bbox[0] + bbox[2] / 2
        bw = bbox[3]
        bh = bbox[2]
        #print 'x %d y %d w %d h %d' % (cx, cy, bw, bh)
        p1 = c[defc_max[0][0]][0]
        p2 = c[defc_max[0][1]][0]
        #print 'DIST', dist(p1, p2)
        if p1[1] < p2[1]:
            pt_fing = p1
        else:
            pt_fing = p2
        #dx = p2[0] - p1[0]
        #dy = p2[1] - p1[1]
        dx = pt_fing[1] - cx
        dy = pt_fing[0] - cy
        return (np.arctan2(dy, dx), pt_fing)

    def get_convex_hulls(self, contours, bbox):
        MIN_CONTOUR_LEN = 100
        curv = []
        defects_max = []
        defects = []
        angles = []
        pt_fings = []
        for c in contours:
            if len(c) > MIN_CONTOUR_LEN:
                hull = cv2.convexHull(c, returnPoints = True)
                hull_ind = cv2.convexHull(c, returnPoints = False)
                curv.append(hull)
                defc = cv2.convexityDefects(c, hull_ind)
                defects.append(defc)
                defc_max = defc[self.get_max_hull_defect_ind(defc, c, bbox)]
                defects_max.append(defc_max)
                angle, pt_fing = self.calc_angle(defc_max, c, bbox)
                angles.append(angle)
                pt_fings.append(pt_fing)
        return curv, defects, defects_max, angles, pt_fings

    def draw_convex_hulls(self, img, conv):
        for c in conv:
            prev_p = c[0]
            for p in c[1:]:
                cv2.line(img, tuple(prev_p[0]), tuple(p[0]), RGB(0, 255, 0))
                prev_p = p

    def draw_fore_info(self, img_fore, bbox, bbox_init, speed, angle, kcurvs, thumb_n_point, mid_fing_pos):
        img_fore = cv2.cvtColor(img_fore, cv2.COLOR_GRAY2BGR)
        fnt = cv2.FONT_HERSHEY_PLAIN
        fnt_size = 2.4
        clr = RGB(255, 0, 0)
        state = 'INIT' if self.state == TrackerState.INIT else 'RUN'
        cv2.putText(img_fore, 'State:  %s' % state, (30, 30) , fnt, fnt_size, clr)
        bbox_str = 'Speed: %03d' % speed
        cv2.putText(img_fore, bbox_str, (30, 60) , fnt, fnt_size, clr)
        #print self.turn_angle_init, angle
        if self.state == TrackerState.RUN:
            turn_str = 'Turn:   %03d deg.' % (int((self.turn_angle_init - angle) * 180 / np.pi))
        else:
            turn_str = 'Turn:   %03d deg.' % 0
        cv2.putText(img_fore, turn_str, (30, 90) , fnt, fnt_size, clr)
        bbox_p1 = (bbox[1], bbox[0])
        bbox_p2 = (bbox[1] + bbox[3], bbox[0] + bbox[2])
        cv2.rectangle(img_fore, bbox_p1, bbox_p2, RGB(0, 255, 0), 5) 
        bbox_cx = bbox[1] + bbox[3] / 2
        bbox_cy = bbox[0] + bbox[2] / 2
        draw_cross(img_fore, (bbox_cx, bbox_cy), 30, RGB(0, 255, 255))
        for i in range(len(kcurvs)):
            kc = kcurvs[i]
            tnp = thumb_n_point[i]
            for j in tnp:
                pass
                #draw_cross(img_fore, kc[j][0], 30, RGB(255, 255, 0))
        if bbox_init:    
            bbox_p1 = (bbox_init[1], bbox_init[0])
            bbox_p2 = (bbox_init[1] + bbox_init[3], bbox_init[0] + bbox_init[2])
            cv2.rectangle(img_fore, bbox_p1, bbox_p2, RGB(255, 0, 0), 5) 
        if self.recog_cnt > 0:
            p1 = (int(self.hand_area[0].x), int(self.hand_area[0].y))
            p2 = (int(self.hand_area[1].x), int(self.hand_area[1].y))
            cv2.rectangle(img_fore, p1, p2, RGB(0, 0, 255), 2) 
        cv2.imwrite('out_fore/img_fore_%03d.png' % self.depth_frame_cnt, img_fore)
        return img_fore

    def draw_debug_info(self, img_orig, img_fore, bbox, bbox_init, conts, img_contours, defects, defects_max, finger_tips, pt_fings, thumb_n_point, mid_fing_pos, kcurvs, hand_kcurv_i, hulls):
        if len(conts) > 0:
            if len(defects_max) > 0:
                if len(defects_max[0]) > 0:
                    p1 = tuple(conts[0][defects_max[0][0][0]][0])
                    p2 = tuple(conts[0][defects_max[0][0][1]][0])
                    if bbox_init == None:
                        bbox_init = (-1, -1, -1, -1)
                    result_str = 'bi %03d %03d %03d %03d b %03d %03d %03d %03d p1 %03d %03d p2 %03d %03d\n' % (bbox_init + bbox + p1 + p2)
                    self.result_file.write(result_str)

        #print finger_tips
        img_curv = img_contours.copy()
        for cf_i in range(len(finger_tips)):
            f = finger_tips[cf_i]
            for p in f:
                cv2.circle(img_contours, tuple(kcurvs[cf_i][p][0]), 10, RGB(0, 255, 0))
            for k in range(len(f)):
                for l in range(len(f)):
                    pass
                    #cv2.line(img_contours, tuple(kcurvs[cf_i][f[k]][0]), tuple(kcurvs[cf_i][f[l]][0]), RGB(255,255,255))
        for i in range(len(hulls)):
            p1 = tuple(conts[i][defects_max[i][0][0]][0])
            p2 = tuple(conts[i][defects_max[i][0][1]][0])
            cv2.line(img_contours, p1, p2, RGB(0, 255, 255))
        cx = bbox[1] + bbox[3] / 2
        cy = bbox[0] + bbox[2] / 2
        draw_cross(img_contours, (cx, cy), 30, RGB(0, 255, 0))
        for pt_fing in pt_fings:
            draw_cross(img_contours, pt_fing, 30, RGB(0, 255, 0))
        '''
        for d_i in range(len(defects)):
            defs = defects[d_i]
            cont = conts[d_i]
            for j in range(len(defs)):
                def_pt = defs[j][0]
                depth_pt_ind = def_pt[2]
                depth_pt = cont[depth_pt_ind][0]
                draw_cross(img_contours, depth_pt, 30, RGB(255, 155, 100))
            #print 'defs', defs
            #print 'cont', cont
        '''
        '''
        if hand_kcurv_i == -1:
            hand_recog = 'No Hand'
        else:
            hand_recog = 'Hand'
        '''
        #draw_debug_messages(img_contours, [self.kcurv_stat_str], (100, 200))
        #draw_debug_messages(img_contours, [hand_recog], (100, 100))
        cv2.imwrite('out_cont/img_contours_%03d.png' % self.depth_frame_cnt, img_contours)
        for i in range(len(kcurvs)):
            kc = kcurvs[i]
            l = len(kc)
            for pt_i in range(1, len(kc) - 1):
                pt0 = kc[pt_i - 1]
                pt1 = kc[pt_i]
                pt2 = kc[pt_i + 1]
                cv2.line(img_curv, tuple(pt0[0]), tuple(pt1[0]), RGB(0, 255, 0))
                cv2.putText(img_curv, '%.3f' % pt1[1], tuple(pt1[0]), cv2.FONT_HERSHEY_PLAIN, 0.7, RGB(0, 255, 0))
                cv2.line(img_curv, tuple(pt1[0]), tuple(pt2[0]), RGB(0, 255, 0))
        cv2.imwrite('out_kcurv/img_kcurvs_%03d.png' % self.depth_frame_cnt, img_curv)

        #self.draw_fore_info(img_fore, bbox, bbox_init, speed, kcurvs, thumb_n_point, mid_fing_pos)
        #print thumb_n_point
       
        img_hulls = 255 * np.ones([480, 640, 3], dtype='uint8')
        self.draw_convex_hulls(img_hulls, hulls)
        cv2.imwrite('out_hull/img_hulls_%03d.png' % self.depth_frame_cnt, img_hulls)
        img_orig_cm = (100 * img_orig * 255 / 200).astype('uint8')
        #cv2.imwrite('out_orig/img_orig_%03d.png' % self.depth_frame_cnt, img_orig_cm)
     

    def filter_nan_depth_simple(self, img):
        img[np.where(np.isnan(img))] = self.MAX_RANGE

    def filter_edge_depth(self, img):
        self.new_img_filter_buf = img
        nans = np.where(np.isnan(img))
        self.new_img_filter_buf[nans] = self.img_filter_buf[nans]
        self.img_filter_buf = self.new_img_filter_buf
        return self.img_filter_buf

    def calc_hist_from_fore(self, img, img_fore):
        hist = Histogram()
        hist.calc(img, user_mask = img_fore)
        back_proj = calc_back_proj(img, hist.hist)
        back_proj &= img_fore
        cv2.imwrite('img_back_%03d.png' % self.depth_frame_cnt, back_proj)

    def rgb_cb(self, msg):
        try:
            img = bridge.imgmsg_to_cv(msg)
            img = np.asarray(img)
        except CvBridgeError as e:
            print >> stderr, 'Cannot convert from ROS msg to CV image:', e
        if self.vwriter_rgb:
            self.vwriter_rgb.write(img)

    def hand_rect_cb(self, hand_rect):
        p1 = hand_rect.p1
        p2 = hand_rect.p2
        self.hand_area = (p1, p2)
        if p1.x == -1.0:
            self.recog_cnt = 0
            self.norecog_cnt += 1
        else:
            print 'HAND:', p1, p2
            self.norecog_cnt = 0
            self.recog_cnt += 1

    def get_kcurv_stat(self, img, fingers):
        if self.kcurv_stat_cnt >= self.kcurv_stat_cnt_max:
            self.kcurv_stat_str = 'Mean %.2f Std. %.2f' % (np.mean(self.kcurv_stat_vals), np.std(self.kcurv_stat_vals))
            self.kcurv_stat_vals = np.array([])
            self.kcurv_stat_cnt = 0
            #print self.kcurv_stat_str
        else:
            self.kcurv_stat_vals = np.append(self.kcurv_stat_vals, len(fingers))
        self.kcurv_stat_cnt += 1

    def get_bounding_box(self, img_fore):
        fore = np.where(img_fore == 255)
        fore_pts = np.array([[[fore[0][i], fore[1][i]]] for i in range(len(fore[0]))], dtype='int32')
        return cv2.boundingRect(fore_pts)

    def reset_state(self):
        self.state = TrackerState.INIT
        self.bbox_init = None
        self.turn_angle_init = 0
        self.recog_cnt = 0
        self.norecog_cnt = 0

    def set_state(self, state):
        self.state = state

    def update_state(self, hand_detected, bbox, angle):
        if not hand_detected:
            #print 'Not Found the hand'
            self.recog_cnt = 0
            self.norecog_cnt += 1
            if self.norecog_cnt >= self.norecog_cnt_min:
                pass
                #self.reset_state()
        else:#found the hand
            #print 'Found the hand'
            self.norecog_cnt = 0
            self.recog_cnt += 1
            if self.recog_cnt == self.recog_cnt_min:
                if not self.bbox_inited:
                    self.bbox_init = bbox
                    self.bbox_inited = True
                self.turn_angle_init = angle
                self.set_state(TrackerState.RUN)
 
    def depth_cb(self, msg):
        self.depth_frame_cnt += 1
        self.state_cnt += 1
        if self.depth_frame_cnt % 3 != 0:
            return
        #print 'recog', self.recog_cnt, 'norecog', self.norecog_cnt
        try:
            img = bridge.imgmsg_to_cv(msg, desired_encoding='32FC1')
            img = np.asarray(img)
        except CvBridgeError as e:
            print >> stderr, 'Cannot convert from ROS msg to CV image:', e

        #print img.shape, msg.encoding
        x = self.mouse_x
        y = self.mouse_y
        self.filter_nan_depth_simple(img)
        #print 'Depth(%d, %d): %f' % (x, y, img[y][x])
        if self.write_dist_hist and self.depth_frame_cnt % 1 == 0:
            self.write_dist_hist_img(img)
        self.img_fore = self.get_foreground_uint8(img)
        bbox = self.get_bounding_box(self.img_fore)
        contours, img_contours = self.get_contours(self.img_fore)
        kcurvs, conts = HandTracker.get_kcurvs(contours, 50)
        contours_fingers, finger_tips, thumb_n_point, mid_fing_pos = HandTracker.get_kcurv_fingers(kcurvs, bbox)
        hand_kcurv_i = self.recognize_hand_kcurv(contours_fingers)
        if len(contours_fingers) > hand_kcurv_i:
            self.get_kcurv_stat(img_contours, contours_fingers[hand_kcurv_i])
        #print 'Contours_fingers', contours_fingers, len(contours_fingers[hand_kcurv_i])
        hulls, defects, defects_max, angles, pt_fings = self.get_convex_hulls(conts, bbox)
        self.draw_debug_info(img, self.img_fore, bbox, self.bbox_init, conts, img_contours, defects, defects_max, finger_tips, pt_fings, thumb_n_point, mid_fing_pos, kcurvs, hand_kcurv_i, hulls)
        #cv2.imshow('wnd_orig', (img * 100).astype('uint8'))
        cv2.imshow('wnd_contours', img_contours)
        ch = cv2.waitKey(10)
        
        hand_recognized = False if hand_kcurv_i == -1 else True
        if self.bbox_init:
            speed = max(0, 100 - int((1.0 * bbox[2] / self.bbox_init[2]) * 100))
        else:
            speed = 0
        self.turn_angle = angles[0] if (len(angles) > 0 and self.norecog_cnt == 0) else UNKNOWN_ANGLE
        self.img_fore = self.draw_fore_info(self.img_fore, bbox, self.bbox_init, speed, self.turn_angle, kcurvs, thumb_n_point, mid_fing_pos)
        
        self.update_state(hand_recognized, bbox, self.turn_angle)
        #Send driving control command to NXT bluetooth driver wrapper
        ctl_point = Point(UNKNOWN_ANGLE, UNKNOWN_ANGLE, UNKNOWN_ANGLE)
        if self.state == TrackerState.RUN and \
           self.norecog_cnt < self.norecog_cnt_min:
            #print 'iangle %.3f cangle %.3f' % (self.turn_angle_init, self.turn_angle)
            if self.turn_angle != UNKNOWN_ANGLE:
                turn_angle = self.turn_angle - self.turn_angle_init
            else:
                turn_angle = CTL_TURN_VALUE_MIN
            ctl_point = Point(speed, turn_angle, 0)
        self.nxt_ctl_pub.publish(ctl_point)
        if self.vwriter_result:
            self.vwriter_result.write(self.img_fore)
        cv2.imshow('wnd_prob', self.img_fore)

        if ch == 27:
            rospy.signal_shutdown('Quit')

    def run(self):
        rospy.spin()

    def on_shutdown(self):
        cv2.destroyAllWindows()
        cv2.waitKey(100)


def hand_tracker(topic_name, thr_mode, use_recognition):
    dpdet = HandTracker(topic_name, thr_mode, use_recognition)
    dpdet.set_rgb_video('rgb_out.avi')
    dpdet.set_result_video('result_out.avi')
    dpdet.run()


def do_track_hand():
    assert len(argv) >= 2, 'USAGE: track_hand.py <threshold_mode> <use_recognition>'
    if argv[1] == 'depth_auto':
        thr_mode = ThresholdMode.DEPTH_AUTO
    elif argv[1] == 'depth_manual':
        thr_mode = ThresholdMode.DEPTH_MANUAL
    else:
        raise Exception('Illegal threshold_mode %s, use DEPTH_AUTO' % argv[1])
    if argv[2] == 'true':
        use_recognition = True
    elif argv[2] == 'false':
        use_recognition = False
    else:
        raise Exception('Illegal use_recognition %s, use true or false' % argv[2])
    hand_tracker(DEF_TOPIC_NAME, thr_mode, use_recognition)


def plot_kcurv(kcurv, fname):
    plt.clf()
    kcurv_coef = [x[1] for x in kcurv]
    plt.title(fname)
    plt.plot(kcurv_coef)
    plt.savefig(fname)

def test2():
    img = np.zeros([320, 240], dtype='uint8')
    l, img = build_line([1, 30], [20, 1], True)
    cv2.imwrite('d.png', img)


def test3():
    img = cv2.imread('d0.png')
    cv2.namedWindow('c0')
    c0 = pickle.load(file('c0'))
    i = 0
    for ci in range(len(c0)):
        c = c0[ci]
        for pi in range(len(c)):
            p = c[pi][0]
            img_i = img.copy()
            s = 'Contour %02d Point %03d of %03d' % (ci, pi, len(c))
            cv2.putText(img_i, s, (10, 30), cv2.FONT_HERSHEY_PLAIN, 1.0, RGB(0, 255, 0))
            cv2.circle(img_i, tuple(p), 10, RGB(0, 255, 255))
            cv2.imshow('c0', img_i)
            cv2.waitKey(300 - int(1200.0 / 300 * len(c)))

    while cv2.waitKey(100) != 27:
        pass

    cv2.destroyWindow('c0')
    cv2.waitKey(100)
    cv2.waitKey(100)


def test4():
    img = cv2.imread('canny.png')
    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    img_ret = max_filter(img)
    cv2.imwrite('filt.png', img_ret)


if __name__ == '__main__':
    prepare_env()
    do_track_hand()
    #test()
    #test_kcurv()
