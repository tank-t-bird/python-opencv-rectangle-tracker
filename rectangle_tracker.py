"""
Notes:
    1) This is algorithm is primarily designed for a rectangular piece of
        paper lying flat on a flat surface, but may work in other situations
        assuming that the paper's corners are not obstructed.
    2) The camera's view of the paper must be unobstructed in first frame.

"""

# Basic Dependencies
from __future__ import division, print_function
from math import ceil, acos
from time import time

# External Dependencies
import numpy as np
from numpy.linalg import norm
import cv2


# Default User Parameters
VIDEO_FILE_LOCATION = 'sample.avi'
FPS_INTERVAL = 10  # updates FPS estimate after this many frames
MHI_DURATION = 10  # max frames remembered by motion history
FRAME_WIDTH, FRAME_HEIGHT = 640, 424
PAPER_RATIO = 11/8.5  # height/width of paper
ROT180 = False  # If paper is upside down, change this
REDUCE_DISPLAY_SIZE = False  # Use if output window is too big for your screen


# Internal Parameters
tol_corner_movement = 1
obst_tol = 10   # used to determine tolerance
closing_iterations = 10
show_thresholding = False  # Use to display thresholding

def rotate180(im):
    """Rotates an image by 180 degrees."""
    return cv2.flip(im, -1)


def persTransform(pts, H):
    """Transforms a list of points, `pts`,
    using the perspective transform `H`."""
    src = np.zeros((len(pts), 1, 2))
    src[:, 0] = pts
    dst = cv2.perspectiveTransform(src, H)
    return np.array(dst[:, 0, :], dtype='float32')


def affTransform(pts, A):
    """Transforms a list of points, `pts`,
    using the affine transform `A`."""
    src = np.zeros((len(pts), 1, 2))
    src[:, 0] = pts
    dst = cv2.transform(src, A)
    return np.array(dst[:, 0, :], dtype='float32')


def draw_polygon(im, vertices, vertex_colors=None, edge_colors=None,
                 alter_input_image=False, draw_edges=True, draw_vertices=True,
                 display=False, title='', pause=False):
    """returns image with polygon drawn on it."""
    _default_vertex_color = (255, 0, 0)
    _default_edge_color = (255, 0, 0)
    if not alter_input_image:
        im2 = im.copy()
    else:
        im2 = im
    if vertices is not None:
        N = len(vertices)
        vertices = [tuple(v) for v in vertices]
        if vertex_colors is None:
            vertex_colors = [_default_vertex_color] * N
        if edge_colors is None:
            edge_colors = [_default_edge_color] * N
        for i in range(N):
            startpt = vertices[(i - 1) % N]
            endpt = vertices[i]
            if draw_vertices:
                cv2.circle(im2, startpt, 3, vertex_colors[(i - 1) % N], -1)
            if draw_edges:
                cv2.line(im2, startpt, endpt, edge_colors[(i - 1) % N], 2)
    if display:
        cv2.imshow(title, im2)
        # Note: `0xFF == ord('q')`is apparently necessary for 64bit machines
        if pause and cv2.waitKey(0) & 0xFF == ord('q'):
            pass
    return im2


def run_main():
    
    # Initialize some variables
    frame = None
    old_homog = None
    old_inv_homog = None
    corner_history = []

    video_feed = cv2.VideoCapture(VIDEO_FILE_LOCATION)
    video_feed.set(cv2.CV_CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    video_feed.set(cv2.CV_CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)

    frame_count = 0
    fps_time = time()
    while True:
        # initialize some stuff
        c_colors = [(0, 0, 255)] * 4

        # grab current frame from video_feed
        previous_frame = frame
        _, frame = video_feed.read()

        # Report FPS
        if not (frame_count % 10):
            fps = FPS_INTERVAL/(time() - fps_time)
            print('Frame:', frame_count, ' | FPS:', fps)
            fps_time = time()
        frame_count += 1

        # Convert to grayscale
        try:
            gray_img = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        except:
            print("\nVideo feed ended.\n")
            break

        # get binary thresholding of image
        gray_smooth = cv2.GaussianBlur(gray_img, (15, 15), 0)
        _, bin_img = cv2.threshold(gray_smooth, 100, 255, cv2.THRESH_BINARY)

        # morphological closing
        kernel = np.ones((3, 3), np.uint8)
        bin_img = cv2.morphologyEx(bin_img, cv2.MORPH_CLOSE,
                                   kernel, iterations=closing_iterations)

        # Find corners.  To do this:
        # 1) Find the largest (area) contour in frame (after thresholding)
        # 2) get contours convex hull,
        # 3) reduce degree of convex hull with Douglas-Peucker algorithm,
        # 4) refine corners with subpixel corner finder

        # step 1
        img, contours, _ = cv2.findContours(bin_img, cv2.RETR_EXTERNAL,
                                                cv2.CHAIN_APPROX_SIMPLE)
        biggest_contour = max(contours, key=cv2.contourArea)

        # step 2
        hull = cv2.convexHull(biggest_contour)
        epsilon = 0.05 * cv2.arcLength(biggest_contour, True)

        # step 3
        hull = cv2.approxPolyDP(hull, epsilon, True)

        # step 4
        hull = np.float32(hull)
        method = cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT
        criteria = (method, 1000, 1e-4)
        cv2.cornerSubPix(gray_img, hull, (5, 5), (-1, -1), criteria)
        corners = [pt[0] for pt in hull]

        # Find top-right corner and use to label corners
        # Note: currently corners are in CW order
        # Note: ordering will be checked below against expected corners
        tr_index = np.argmin(c[0] + c[1] for c in corners)
        tl = corners[tr_index]
        bl = corners[(tr_index - 1) % 4]
        br = corners[(tr_index - 2) % 4]
        tr = corners[(tr_index - 3) % 4]

        # reformat and ensure that ordering is as expected below
        corners = np.float32([[c[0], c[1]] for c in [tl, bl, br, tr]])

        # IMPORTANT ASSUMPTIONS on paper tracking (used in code block below):
        # 1) if any one point is stationary from previous frame, then all
        #    are stationary with probability 1.
        # 2) if any corners are obstructed, assume the paper is still flat
        #    against the same plane as it was in the previous frame.
        #    I.e. the transformation from previous frame to this frame should
        #    be of the form of a translation and a rotation in said plane.
        # 3) see code comments for additional assumptions, haha, sorry

        def get_edge_lengths(topl, botl, botr, topr):
            """ Takes in list of four corners, returns four edge lengths
            in order top, right, bottom, left."""
            tbrl = [topr - topl, topr - botr, botr - botl, botl - topl]
            return [norm(edge) for edge in tbrl]

        if not corner_history:
            last_unob_corners = corners
        else:
            # determine expected corner locations and edge lengths
            expected_corners = last_unob_corners
            if last_unob_corners is None:
                expected_lengths = get_edge_lengths(*expected_corners)
            else:
                expected_lengths = get_edge_lengths(*last_unob_corners)

            # check ordering
            def cyclist(lst, k):
                if k:
                    return [lst[(i+k)%len(lst)] for i in xrange(len(lst))]
                return lst

            def _order_dist(offset):
                offset_corners = corners[cyclist(range(4), offset)]
                return norm(expected_corners - offset_corners), offset_corners
            if corner_history:
                corners = min(_order_dist(k) for k in range(4))[1]

            # Look for obstructions by looking for changes in edge lengths
            # Note: these lengths are not perspective invariant
            # TODO: checking by Hessian may be a better method
            new_lengths = get_edge_lengths(*corners)
            top_is_bad, rgt_is_bad, bot_is_bad, lft_is_bad = \
                [abs(l0 - l1) > obst_tol for l1, l0 in
                                zip(new_lengths, expected_lengths)]
            tl_ob = top_is_bad and lft_is_bad
            bl_ob = bot_is_bad and lft_is_bad
            br_ob = bot_is_bad and rgt_is_bad
            tr_ob = top_is_bad and rgt_is_bad

            is_obstr = [tl_ob, bl_ob, br_ob, tr_ob]
            ob_indices = [i for i, c in enumerate(is_obstr) if c]
            ob_corner_ct = sum(is_obstr)
            c_colors = [(0, 255, 0) if b else (0, 0, 255) for b in is_obstr]

            # Find difference of corners from expected location
            diffs = [norm(c - ec) for c, ec in zip(corners, expected_corners)]
            has_moved = [d > tol_corner_movement for d in diffs]

            # Check if paper has likely moved
            if sum(has_moved) < 4:
                # assume all is cool, just trust the corners found
                corners = last_unob_corners
                pass
            else:
                if sum(has_moved) == 1:
                    # only one corner has moved, just assume it's obstructed
                    # and replace it with the expected location
                    bad_corner_idx = np.argmax(diffs)
                    corners[bad_corner_idx] = expected_corners[bad_corner_idx]

                else:  # find paper's affine transformation in expected plane
                    print("frame={} | ob_corner_ct={}"
                          "".format(frame_count, ob_corner_ct))

                    if sum(is_obstr) in (1, 2, 3):
                        eco = zip(expected_corners, is_obstr)
                        exp_unob = np.float32([c for c, b in eco if not b])
                        exp_ob = np.float32([c for c, b in eco if b])
                        co = zip(corners, is_obstr)
                        new_unob = np.float32([c for c, b in co if not b])

                        exp_unob_pp = persTransform(exp_unob, old_homog)
                        exp_ob_pp = persTransform(exp_ob, old_homog)
                        new_unob_pp = persTransform(new_unob, old_homog)

                    # check for obstructions
                    if sum(is_obstr) == 0:  # yay! no obstructed corners!
                        pass

                    elif sum(is_obstr) == 1:
                        # Find the affine transformation in the paper's plane
                        # from expected locations of the three unobstructed
                        # corners to the found locations, then use this to
                        # estimate the obstructed corner's location
                        A = cv2.getAffineTransform(exp_unob_pp, new_unob_pp)
                        new_ob_pp = affTransform(exp_ob_pp, A)
                        new_ob = persTransform(new_ob_pp, old_inv_homog)
                        corners[np.ix_(ob_indices)] = new_ob

                    elif sum(is_obstr) == 2:
                        # Align the line between the good corners
                        # with the same line w.r.t the old corners
                        p1, q1 = new_unob_pp[0], new_unob_pp[1]
                        p0, q0 = exp_unob_pp[0], exp_unob_pp[1]
                        u0 = (q0 - p0) / norm(q0 - p0)
                        u1 = (q1 - p1) / norm(q1 - p1)
                        angle = acos(np.dot(u0, u1))  # unsigned
                        trans = p1 - p0

                        # Find rotation that moves u0 to u1
                        rotat = cv2.getRotationMatrix2D(tuple(p1), angle, 1)
                        rotat = rotat[:, :2]

                        # Expensive sign check for angle (could be improved)
                        if norm(np.dot(u0, rotat) - u1) > norm(np.dot(u1, rotat) - u0):
                            rotat = np.linalg.inv(rotat)

                        # transform the old coords of the hidden corners
                        # and map them back to the paper plane
                        exp_ob_pp += trans
                        new_ob_pp = affTransform(exp_ob_pp, rotat)
                        new_ob = persTransform(new_ob_pp, old_inv_homog)
                        corners[np.ix_(ob_indices)] = new_ob

                    elif sum(is_obstr) in (3, 4):
                        print("Uh oh, {} corners obstructed..."
                              "".format(ob_corner_ct))
                        corners = expected_corners
                    else:
                        raise Exception("This should never happen.")


        # Homography
        w = max(abs(br[0] - bl[0]),
                abs(tr[0] - tl[0]))  # width of paper in pixels
        h = PAPER_RATIO * w
        corners_pp = np.float32([[0, 0], [0, h], [w, h], [w, 0]])
        homog, mask = cv2.findHomography(corners, corners_pp)
        inv_homog, inv_mask = cv2.findHomography(corners_pp, corners)
        paper = cv2.warpPerspective(frame, homog, (int(ceil(w)), int(ceil(h))))
        if ROT180:
            paper = rotate180(paper)


        # Draw detected paper boundary on frame
        segmented_frame = draw_polygon(frame, corners, c_colors)

        # Resize paper to simplify display
        h = segmented_frame.shape[0]
        paper_w = int(round(h*paper.shape[1]/paper.shape[0]))
        resized_paper = cv2.resize(paper, (paper_w, h))

        # Display
        big_img = np.hstack((segmented_frame, resized_paper))
        if show_thresholding:
            bin_img = cv2.cvtColor(bin_img, cv2.COLOR_GRAY2BGR)
            big_img = np.hstack((big_img, bin_img))
        if REDUCE_DISPLAY_SIZE:
            reduced_size = tuple(np.array(big_img.shape[:2][::-1])//2)
            smaller_big_img = cv2.resize(big_img, reduced_size)
        cv2.imshow('', big_img)


        # Updates for next iteration
        corner_history.append(corners)
        old_homog = homog
        old_inv_homog = inv_homog


        # this is apparently necessary for 64bit machines
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    video_feed.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    try:
        run_main()
    except:
        cv2.destroyAllWindows()
        raise
