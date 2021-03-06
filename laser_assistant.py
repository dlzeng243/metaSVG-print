#!/usr/bin/env python3
# laser_assistant.py
"""A tool to generate joints for laser cutting"""
import xml.etree.ElementTree as ET
import json
import math

from laser_path_utils import (get_length, get_start, get_angle,
                              move_path, path_string_to_points, rotate_path, scale_path,
                              get_overlapping, get_not_overlapping,
                              paths_to_loops, loops_to_paths,
                              separate_closed_paths, is_inside,
                              path_to_segments)
from laser_clipper import get_difference, get_offset_loop, get_union
import svgpathtools as SVGPT
from laser_svg_parser import separate_perims_from_cuts, parse_svgfile, model_to_svg_file
# from joint_generators import FlatJoint, BoxJoint, TslotJoint

#used when run from the command line:
import argparse, csv, sys


class LaserParameters:
    def __init__(self, d):
        """Read parameters from a dictionary; throw on missing parameters"""

        #material sheet size:
        self.thickness = float(d['thickness'])
        self.width = float(d['width'])
        self.height = float(d['height'])

        #compensate for cut width:
        self.kerf = float(d['kerf'])

        def floatOrNan(val):
            #This is a hack to get around our sheet having blank cells and cells with "NA" in them:
            # TODO: fix the sheet and remove this hack!
            if val == 'NA': return float('nan')
            elif val == '': return 0.0
            else: return float(val)

        #joint fit parameters:
        self.boxC = floatOrNan(d['boxC'])
        self.boxL = floatOrNan(d['boxL'])
        self.boxI = floatOrNan(d['boxI'])
        self.tabC = floatOrNan(d['tabC'])
        self.tabL = floatOrNan(d['tabL'])
        self.tabI = floatOrNan(d['tabI'])
        self.slotC = floatOrNan(d['slotC'])
        self.slotL = floatOrNan(d['slotL'])
        self.slotI = floatOrNan(d['slotI'])

        #perhaps....
        #self.clearance = float(d['clearance']) #generic "clearance" value for flat joints?

        #output style (appended to svg's style):
        self.style = d['style']

        #misc (for preset system):
        self.preset = d['preset']
        self.notes = d['notes']

        #scale is optional:
        if 'scale' in d:
            self.scale = float(d['scale'])
        else:
            self.scale = 1.0

    FIT_MAP = {
        'Clearance':'C',
        'Friction':'L',
        'Press':'I'
    }
    def get_fit(self, joint, fit):
        return getattr(self, joint + self.FIT_MAP[fit])



def make_blank_model(attrib=None):
    """Make a valid blank model"""
    if attrib is None:
        attrib = {}
    if "id" in attrib:
        del attrib['id']
    attrib['xmlns'] = "http://www.w3.org/2000/svg"

    model = {'tree': {}, 'attrib': attrib}
    return model


def place_new_edge_path(new_edge_path, old_edge_path):
    """moves and rotates new path to line up with old path"""
    # assert get_angle(new_edge_path) == 0

    start_point = get_start(old_edge_path)
    moved_path = move_path(new_edge_path, start_point)

    rotation_angle = get_angle(old_edge_path)
    rotated_path = rotate_path(moved_path, rotation_angle, start_point)

    return rotated_path


def process_edge(a_or_b, edge, parameters):
    """Generates, translates and rotates joint path into place"""
    assert 'paths' in edge
    assert len(edge['paths']) == 1

    old_edge_path = edge['paths'][0]
    parameters['length'] = get_length(old_edge_path)
    generator = parameters['generator']()

    new_edge_path = generator.make(a_or_b, parameters)
    placed_path = place_new_edge_path(new_edge_path, old_edge_path)

    return placed_path


def subtract_geometry(perimeters, cuts):
    """subtracts cuts from faces"""
    perimeters_loops = paths_to_loops(perimeters)
    cuts_loops = paths_to_loops(cuts)
    differnce_loops = get_difference(perimeters_loops, cuts_loops)
    differnce = loops_to_paths(differnce_loops)
    # differnce = loops_to_paths(cuts_loops)
    return differnce


def combine_geometry(first, second):
    """combines two path lists"""
    first_loops = paths_to_loops(first)
    second_loops = paths_to_loops(second)
    combined_loops = get_union(first_loops, second_loops)
    combined = loops_to_paths(combined_loops)
    # differnce = loops_to_paths(cuts_loops)
    return combined

# def get_original(tree):
#     """returns paths of original and target geometry"""
#     original_style = "fill:#00ff00;fill-opacity:0.1;stroke:#000000;" + \
#         f"stroke-linejoin:round;stroke-width:0px"

#     for face, shapes in tree.items():
#         if face.startswith('face'):
#             perimeters = []
#             perimeter_paths = shapes['Perimeter']['paths']
#             for path in perimeter_paths:
#                 perimeters.append(path)
#             cuts = []
#             cut_paths = shapes['Cuts']['paths']
#             if cut_paths != []:
#                 for path in cut_paths:
#                     cuts.append(path)
#                 tree[face]['Original'] = {
#                     'paths': subtract_geometry(perimeters, cuts),
#                     'style': original_style}
#             else:
#                 tree[face]['Original'] = {
#                     'paths': perimeters,
#                     'style': original_style}
#     return tree


def get_original_tree(model):
    """returns paths (zero stroke with green fill) of original model"""
    tree = {}
    for face, shapes in model['tree'].items():
        if face.startswith('face'):
            perimeters = []
            perimeter_paths = shapes['Perimeter']['paths']
            for path in perimeter_paths:
                perimeters.append(path)
            cuts = []
            cut_paths = shapes['Cuts']['paths']
            if cut_paths != []:
                for path in cut_paths:
                    cuts.append(path)
                tree[face] = {
                    'paths': subtract_geometry(perimeters, cuts)}
            else:
                tree[face] = {
                    'paths': perimeters}
    return tree


def process_joints(model, joints, parameters):
    """takes in model of paces and returns modified model with joints applied"""
    for _, joint in joints.items():
        extensions = get_joint_adds(joint, model, parameters)
        for face, extension in extensions.items():
            model['tree'][face]['paths'] = combine_geometry(
                model['tree'][face]['paths'], extension)

    for _, joint in joints.items():
        cuts = get_joint_cuts(joint, model, parameters)
        for face, cut in cuts.items():
            model['tree'][face]['paths'] = subtract_geometry(
                model['tree'][face]['paths'], cut)
    return model

def get_slotted_joint_adds(joint, _, parameters):
    """generator for slotted joints"""
    """Nothing to add for slotted"""
    adds = {}

    return adds

def get_slotted_joint_cuts(joint, _, parameters):
    """genereator for slotted joints"""
    """Cut out the slots"""
    cuts = {}
    thickness = parameters.thickness

    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']

    alignment = joint['joint_parameters']['joint_align']
    fit = parameters.get_fit('slot', joint['joint_parameters']['fit'])

    intersection = joint['joint_parameters']['intersection']
    percentage = joint['joint_parameters']['percentage']
    epsilon = 0.000001
    print(intersection)
    print(percentage)
    tabdist1 = joint['joint_parameters']['tabDist1']
    tabdist2 = joint['joint_parameters']['tabDist2']
    tabslopex, tabslopey = joint['joint_parameters']['tabSlope']
    '''pointsa = path_string_to_points(patha) 
    starta = patha[0]
    enda = patha[len(patha) - 1]'''
    angledtab = thickness
    if abs(tabslopex) > epsilon:
        angle = math.sin(math.atan(tabslopey / tabslopex))
        if abs(angle) > epsilon:
            angledtab = abs(thickness / angle)
    '''if tabslopey > 0:
        tabslopex *= -1
        tabslopey *= -1
    print(tabdist1)
    print(tabdist2)
    print(angledtab)
    cuta = f"M {0} {0} " + \
           f"L {tabdist1} {0}" + \
           f"L {tabdist1 + intersection * percentage * tabslopex} {intersection * percentage * tabslopey}" + \
           f"L {tabdist1 + intersection * percentage * tabslopex + (tabslopey * thickness)} {intersection * percentage * tabslopey + (-tabslopex * thickness)}" + \
           f"L {tabdist1 + angledtab} {0}" + \
           f"L {tabdist1 + tabdist2 + angledtab} {0}"'''
    cuta = f"M {0} {0} " + \
           f"L {tabdist1} {0}" + \
           f"L {tabdist1} {intersection * percentage}" + \
           f"L {tabdist1 + angledtab} {intersection * percentage}" + \
           f"L {tabdist1 + angledtab} {0}" + \
           f"L {tabdist1 + tabdist2 + angledtab} {0}"
    basedist1 = joint['joint_parameters']['baseDist1']
    basedist2 = joint['joint_parameters']['baseDist2']
    baseslopex, baseslopey = joint['joint_parameters']['baseSlope']

    '''pointsb = path_string_to_points(pathb) 
    startb = pathb[0]
    endb = pathb[len(pathb) - 1]'''
    angledbase = thickness
    if abs(baseslopex) > epsilon:
        angle = math.sin(math.atan(baseslopey / baseslopex))
        if abs(angle) > epsilon:
            angledbase = abs(thickness / angle)
    '''if baseslopey > 0:
        baseslopex *= -1
        baseslopey *= -1
    print(basedist1)
    print(basedist2)
    print(angledbase)
    print("\n")
    print(joint['joint_parameters']['tabSlope'])
    print(joint['joint_parameters']['baseSlope'])
    print("\n")
    cutb = f"M {0} {0} " + \
           f"L {basedist1} {0}" + \
           f"L {basedist1 + intersection * (1-percentage) * baseslopex} {intersection * (1-percentage) * baseslopey}" + \
           f"L {basedist1 + intersection * (1-percentage) * baseslopex + (baseslopey * thickness)} {intersection * (1-percentage) * baseslopey + (-baseslopex * thickness)}" + \
           f"L {basedist1 + angledbase} {0}" + \
           f"L {basedist1 + basedist2 + angledbase} {0}"
    print(cuta)
    print(cutb)
    print("\n")'''
    cutb = f"M {0} {0} " + \
           f"L {basedist1} {0}" + \
           f"L {basedist1} {intersection * (1-percentage)}" + \
           f"L {basedist1 + angledbase} {intersection * (1-percentage)}" + \
           f"L {basedist1 + angledbase} {0}" + \
           f"L {basedist1 + basedist2 + angledbase} {0}"
    lengtha = get_length(patha)
    lengthb = get_length(pathb)

    cuta = align_joint(cuta, lengtha, thickness, alignment)
    cutb = align_joint(cutb, lengthb, thickness, alignment)
    '''
    print(patha)
    print(pathb)
    print("\n")
    print(cuta)
    print(cutb)'''
    cuts[facea] = [place_new_edge_path(cuta, patha)]
    cuts[faceb] = [place_new_edge_path(cutb, pathb)]
    '''
    print("\n")
    print(cuts[facea])
    print(cuts[faceb])'''

    return cuts

def get_box_joint_adds(joint, _, parameters):
    """generator for box joints"""
    adds = {}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    adda = f"M {0} {0} "+f"L {0} {thickness} " + \
        f"L {lengtha} {thickness} "+f"L {lengtha} {0}"
    addb = f"M {0} {0} "+f"L {0} {thickness} " + \
        f"L {lengthb} {thickness} " + f"L {lengthb} {0}"

    adda = align_joint(adda, lengtha, thickness, alignment)
    addb = align_joint(addb, lengthb, thickness, alignment)

    adds[facea] = [place_new_edge_path(adda, patha)]
    adds[faceb] = [place_new_edge_path(addb, pathb)]

    return adds

def get_box_joint_cuts(joint, _, parameters):
    """generator for box joints"""
    cuts = {}
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    tabsize = joint['joint_parameters']['tabsize']
    tabspace = joint['joint_parameters']['tabspace']
    tabnum = joint['joint_parameters']['tabnum']
    alignment = joint['joint_parameters']['joint_align']

    fit = parameters.get_fit('box', joint['joint_parameters']['fit'])

    sega = tabsize*tabnum + tabspace*(tabnum-1) - fit
    segb = tabsize*tabnum + tabspace*(tabnum-1) + fit
    offseta = (lengtha - sega) / 2.0
    offsetb = (lengthb - segb) / 2.0

    cuta = f""
    position = offseta
    for _ in range(tabnum):
        cuta += f"M {position} {0} " + \
                f"L {position} {thickness}" + \
                f"L {position + tabsize - fit} {thickness}" + \
                f"L {position + tabsize - fit} {0} Z "
        position = position + tabsize+tabspace

    cutb = f"M {0} {0} " + f"L {0} {thickness} " + \
           f"L {offsetb} {thickness} " + f"L {offsetb} {0} Z "
    position = offsetb
    step = tabsize + tabspace
    for _ in range(tabnum - 1):
        cutb += f"M {position+tabsize+fit} {0} " + f"L {position+tabsize+fit} {thickness} " + \
                f"L {position+tabsize+tabspace} {thickness} " + \
                f"L {position+tabsize+tabspace} {0} Z "
        position = position + step
    position = position + tabsize
    cutb += f"M {position+fit} {0} " + f"L {position+fit} {thickness} " + \
            f"L {lengthb} {thickness} " + f"L {lengthb} {0} Z "

    cuta = align_joint(cuta, lengtha, thickness, alignment)
    cutb = align_joint(cutb, lengthb, thickness, alignment)

    cuts[facea] = [place_new_edge_path(cuta, patha)]
    cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_bolt_joint_adds(joint, _, parameters):
    """generator for bolt joints"""
    adds = {}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    adda = f"M {0} {thickness} "+f"L {0} {-thickness} " + \
        f"L {lengtha} {-thickness} "+f"L {lengtha} {thickness}"
    addb = f"M {0} {0} "+f"L {0} {thickness} " + \
        f"L {lengthb} {thickness} "+f"L {lengthb} {0}"

    adda = align_joint(adda, lengtha, thickness, alignment)
    addb = align_joint(addb, lengthb, thickness, alignment)

    adds[facea] = [place_new_edge_path(adda, patha)]
    adds[faceb] = [place_new_edge_path(addb, pathb)]

    return adds


def get_bolt_joint_cuts(joint, _, parameters):
    """generator for bolt joints"""
    cuts = {}

    nut_bolt_sizes = {'M2': {'nut_width': 3.3,
                             'nut_height': 2.0,
                             'bolt_diameter': 2},
                      'M2.5': {'nut_width': 4.3,
                               'nut_height': 2.0,
                               'bolt_diameter': 2.5},
                      'M3': {'nut_width': 5.5,
                             'nut_height': 2.0,
                             'bolt_diameter': 3.0},
                      'M4': {'nut_width': 7.0,
                             'nut_height': 2.0,
                             'bolt_diameter': 4.0}}
    clearance = 0.1

    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    bolt_size = joint['joint_parameters']['boltsize']
    bolt_space = joint['joint_parameters']['boltspace']
    bolt_num = joint['joint_parameters']['boltnum']
    bolt_length = joint['joint_parameters']['boltlength']

    nut_width = nut_bolt_sizes[bolt_size]['nut_width'] + clearance
    nut_height = nut_bolt_sizes[bolt_size]['nut_height'] + clearance
    bolt_diameter = nut_bolt_sizes[bolt_size]['bolt_diameter'] + clearance

    segment_length = nut_width * 3
    combined_length = bolt_num * segment_length + \
        bolt_space * (bolt_num - 1)
    buffer_size_a = (lengtha - combined_length) / 2
    buffer_size_b = (lengthb - combined_length) / 2

    x_0 = 0
    x_1 = (nut_width - bolt_diameter) / 2
    x_2 = (nut_width + bolt_diameter) / 2
    x_3 = nut_width

    y_0 = 0
    # y_1 = thickness
    y_2 = bolt_length - (2*nut_height)
    y_3 = bolt_length - nut_height
    y_4 = bolt_length

    cuts[facea] = []
    # cuta = f""
    position = buffer_size_a
    for _ in range(bolt_num):
        cuta = f"M {position} {0} " + \
            f"L {position} {thickness} " + \
            f"L {position+nut_width} {thickness} " + \
            f"L {position+nut_width} {0} " + \
            f"L {position} {0} "
        cuta = align_joint(cuta, lengtha, thickness, alignment)
        cuts[facea].append(place_new_edge_path(cuta, patha))
        cuta = f"M {position+nut_width+x_1} {thickness/2} " + \
            f"A {bolt_diameter/2} {bolt_diameter/2} 0 0 1 " + \
            f"{position+nut_width+x_1 + bolt_diameter} {thickness/2} " + \
            f"M {position+nut_width+x_1 + bolt_diameter} {thickness/2} " + \
            f"A {bolt_diameter/2} {bolt_diameter/2} 0 0 1 " + \
            f"{position+nut_width+x_1} {thickness/2} "
        cuta = align_joint(cuta, lengtha, thickness, alignment)
        cuts[facea].append(place_new_edge_path(cuta, patha))
        cuta = f"M {position+2*nut_width} {0} " + \
            f"L {position+2*nut_width} {thickness} " + \
            f"L {position+nut_width+2*nut_width} {thickness} " + \
            f"L {position+nut_width+2*nut_width} {0} Z "
        cuta = align_joint(cuta, lengtha, thickness, alignment)
        cuts[facea].append(place_new_edge_path(cuta, patha))
        position = position + bolt_space + segment_length

    # cuta += f"M {position} {0} " + \
    #         f"L {position} {thickness} " + \
    #         f"L {position+nut_width} {thickness} " + \
    #         f"L {position+nut_width} {0} Z "
    cuts[faceb] = []
    # cuta = f""
    cutb = f"M {0} {0} " + \
        f"L {0} {thickness} " + \
        f"L {buffer_size_b} {thickness} " + \
        f"L {buffer_size_b} {0} Z "
    cutb += f"M {lengthb} {0} " + \
        f"L {lengthb} {thickness} " + \
        f"L {lengthb - buffer_size_b} {thickness} " + \
        f"L {lengthb - buffer_size_b} {0} Z "
    cutb = align_joint(cutb, lengthb, thickness, alignment)
    cuts[faceb].append(place_new_edge_path(cutb, pathb))

    position = buffer_size_b
    for bolt in range(bolt_num):
        cutb = f"M {position+nut_width} {0} " + \
            f"L {position+nut_width} {thickness} " + \
            f"L {position+nut_width*2} {thickness} " + \
            f"L {position+nut_width*2} {0} Z "
        cutb = align_joint(cutb, lengthb, thickness, alignment)
        cuts[faceb].append(place_new_edge_path(cutb, pathb))
        if bolt < bolt_num:
            cutb = f"M {position+segment_length} {0} " + \
                f"L {position+segment_length} {thickness} " + \
                f"L {position+segment_length+bolt_space} {thickness} " + \
                f"L {position+segment_length+bolt_space} {0} Z "
            cutb = align_joint(cutb, lengthb, thickness, alignment)
            cuts[faceb].append(place_new_edge_path(cutb, pathb))
        position = position + bolt_space + segment_length

    position = buffer_size_b
    for bolt in range(bolt_num):
        cutb = f"M {position+nut_width+x_1} {y_0} " + \
            f"L {position+nut_width+x_1} {y_2} " + \
            f"L {position+nut_width+x_0} {y_2} " + \
            f"L {position+nut_width+x_0} {y_3} " + \
            f"L {position+nut_width+x_1} {y_3} " + \
            f"L {position+nut_width+x_1} {y_4} " + \
            f"L {position+nut_width+x_2} {y_4} " + \
            f"L {position+nut_width+x_2} {y_3} " + \
            f"L {position+nut_width+x_3} {y_3} " + \
            f"L {position+nut_width+x_3} {y_2} " + \
            f"L {position+nut_width+x_2} {y_2} " + \
            f"L {position+nut_width+x_2} {y_0} Z "
        cutb = align_joint(cutb, lengthb, thickness, alignment)
        cuts[faceb].append(place_new_edge_path(cutb, pathb))
        position = position + bolt_space + segment_length

    #cutb = f"M {lengt} {0} L {0} {thickness} L {buffer_size_b} {thickness} L {buffer_size_b} {0} Z"

    # cuts[facea] = [place_new_edge_path(cuta, patha)]
    # cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_tslot_joint_adds(joint, _, parameters):
    """generator for tslot joints"""
    # https://docs.google.com/spreadsheets/d/1WmfN8BqZF7OF0b_wrQmnSpbe4QhH35GthL3uRCC2ex8/edit#gid=0
    adds = {}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    adda = f"M {0} {thickness} "+f"L {0} {-thickness} " + \
        f"L {lengtha} {-thickness} "+f"L {lengtha} {thickness}"
    addb = f"M {0} {0} "+f"L {0} {thickness} " + \
        f"L {lengthb} {thickness} "+f"L {lengthb} {0}"

    adda = align_joint(adda, lengtha, thickness, alignment)
    addb = align_joint(addb, lengthb, thickness, alignment)

    adds[facea] = [place_new_edge_path(adda, patha)]
    adds[faceb] = [place_new_edge_path(addb, pathb)]

    return adds


def get_tslot_joint_cuts(joint, _, parameters):
    """generator for tslot joints"""
    cuts = {}

    nut_bolt_sizes = {'M2': {'nut_width': 3.3,
                             'nut_height': 2.0,
                             'bolt_diameter': 2},
                      'M2.5': {'nut_width': 4.3,
                               'nut_height': 2.0,
                               'bolt_diameter': 2.5},
                      'M3': {'nut_width': 5.5,
                             'nut_height': 2.0,
                             'bolt_diameter': 3.0},
                      'M4': {'nut_width': 7.0,
                             'nut_height': 2.0,
                             'bolt_diameter': 4.0}}
    clearance = 0.1

    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    bolt_size = joint['joint_parameters']['boltsize']
    bolt_space = joint['joint_parameters']['boltspace']
    bolt_num = joint['joint_parameters']['boltnum']
    bolt_length = joint['joint_parameters']['boltlength']

    nut_width = nut_bolt_sizes[bolt_size]['nut_width'] + clearance
    nut_height = nut_bolt_sizes[bolt_size]['nut_height'] + clearance
    bolt_diameter = nut_bolt_sizes[bolt_size]['bolt_diameter'] + clearance

    segment_length = nut_width * 3
    combined_length = bolt_num * segment_length + \
        bolt_space * (bolt_num - 1)
    buffer_size_a = (lengtha - combined_length) / 2
    buffer_size_b = (lengthb - combined_length) / 2

    x_0 = 0
    x_1 = (nut_width - bolt_diameter) / 2
    x_2 = (nut_width + bolt_diameter) / 2
    x_3 = nut_width

    y_0 = 0
    # y_1 = thickness
    y_2 = bolt_length - (2*nut_height)
    y_3 = bolt_length - nut_height
    y_4 = bolt_length

    cuts[facea] = []
    # cuta = f""
    position = buffer_size_a
    for _ in range(bolt_num):
        cuta = f"M {position+nut_width+x_1} {thickness/2} " + \
            f"A {bolt_diameter/2} {bolt_diameter/2} 0 0 1 " + \
            f"{position+nut_width+x_1 + bolt_diameter} {thickness/2} " + \
            f"M {position+nut_width+x_1 + bolt_diameter} {thickness/2} " + \
            f"A {bolt_diameter/2} {bolt_diameter/2} 0 0 1 " + \
            f"{position+nut_width+x_1} {thickness/2} "
        cuta = align_joint(cuta, lengtha, thickness, alignment)
        cuts[facea].append(place_new_edge_path(cuta, patha))
        position = position + bolt_space + segment_length

    # cuta += f"M {position} {0} " + \
    #         f"L {position} {thickness} " + \
    #         f"L {position+nut_width} {thickness} " + \
    #         f"L {position+nut_width} {0} Z "
    cuts[faceb] = []
    # cuta = f""

    position = buffer_size_b
    for bolt in range(bolt_num):
        cutb = f"M {position+nut_width+x_1} {y_0} " + \
            f"L {position+nut_width+x_1} {y_2} " + \
            f"L {position+nut_width+x_0} {y_2} " + \
            f"L {position+nut_width+x_0} {y_3} " + \
            f"L {position+nut_width+x_1} {y_3} " + \
            f"L {position+nut_width+x_1} {y_4} " + \
            f"L {position+nut_width+x_2} {y_4} " + \
            f"L {position+nut_width+x_2} {y_3} " + \
            f"L {position+nut_width+x_3} {y_3} " + \
            f"L {position+nut_width+x_3} {y_2} " + \
            f"L {position+nut_width+x_2} {y_2} " + \
            f"L {position+nut_width+x_2} {y_0} Z "
        cutb = align_joint(cutb, lengthb, thickness, alignment)
        cuts[faceb].append(place_new_edge_path(cutb, pathb))
        position = position + bolt_space + segment_length

    #cutb = f"M {lengt} {0} L {0} {thickness} L {buffer_size_b} {thickness} L {buffer_size_b} {0} Z"

    # cuts[facea] = [place_new_edge_path(cuta, patha)]
    # cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_tabslot_joint_adds(joint, _, parameters):
    """generator for tabslot joints"""
    adds = {}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    adda = f"M {0} {thickness} "+f"L {0} {-thickness} " + \
        f"L {lengtha} {-thickness} "+f"L {lengtha} {thickness}"
    addb = f"M {0} {0} "+f"L {0} {thickness} " + \
        f"L {lengthb} {thickness} " + f"L {lengthb} {0}"

    adda = align_joint(adda, lengtha, thickness, alignment)
    addb = align_joint(addb, lengthb, thickness, alignment)

    adds[facea] = [place_new_edge_path(adda, patha)]
    adds[faceb] = [place_new_edge_path(addb, pathb)]

    return adds


def get_tabslot_joint_cuts(joint, _, parameters):
    """generator for tabslot joints"""
    cuts = {}
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness * math.sin(angle)

    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    tabsize = joint['joint_parameters']['tabsize']
    tabspace = joint['joint_parameters']['tabspace']
    tabnum = joint['joint_parameters']['tabnum']
    thickness = parameters.thickness - \
        parameters.get_fit('tab', 'Clearance') #TODO: why?
    fit = parameters.get_fit('tab', joint['joint_parameters']['fit'])
    alignment = joint['joint_parameters']['joint_align']

    sega = tabsize*tabnum + tabspace*(tabnum-1) - fit
    segb = tabsize*tabnum + tabspace*(tabnum-1) + fit
    offseta = (lengtha - sega) / 2.0
    offsetb = (lengthb - segb) / 2.0

    cuta = f""
    position = offseta
    for _ in range(tabnum):
        cuta += f"M {position} {0} " + \
                f"L {position} {thickness}" + \
                f"L {position + tabsize - fit} {thickness}" + \
                f"L {position + tabsize - fit} {0} Z "
        position = position + tabsize+tabspace

    cutb = f"M {0} {0} " + f"L {0} {thickness} " + \
           f"L {offsetb} {thickness} " + f"L {offsetb} {0} Z "
    position = offsetb
    step = tabsize + tabspace
    for _ in range(tabnum - 1):
        cutb += f"M {position+tabsize+fit} {0} " + f"L {position+tabsize+fit} {thickness} " + \
                f"L {position+tabsize+tabspace} {thickness} " + \
                f"L {position+tabsize+tabspace} {0} Z "
        position = position + step
    position = position + tabsize
    cutb += f"M {position+fit} {0} " + f"L {position+fit} {thickness} " + \
            f"L {lengthb} {thickness} " + f"L {lengthb} {0} Z "

    cuta = align_joint(cuta, lengtha, thickness, alignment)
    cutb = align_joint(cutb, lengthb, thickness, alignment)

    cuts[facea] = [place_new_edge_path(cuta, patha)]
    cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_interlock_joint_adds(joint, _, parameters):
    """generator for interlock joints"""
    adds = {}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    alignment = joint['joint_parameters']['joint_align']

    adda = f"M {0} {thickness} "+f"L {0} {-thickness} " + \
        f"L {lengtha} {-thickness} "+f"L {lengtha} {thickness}"
    addb = f"M {0} {thickness} "+f"L {0} {-thickness} " + \
        f"L {lengthb} {-thickness} " + f"L {lengthb} {thickness}"

    adda = align_joint(adda, lengtha, thickness, alignment)
    addb = align_joint(addb, lengthb, thickness, alignment)

    adds[facea] = [place_new_edge_path(adda, patha)]
    adds[faceb] = [place_new_edge_path(addb, pathb)]

    return adds


def get_interlock_joint_cuts(joint, _, parameters):
    """generator for interlock joints"""
    cuts = {}
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    if thickness > 4.5:
        fits = {'Wood': {'Clearance': -0.05, 'Friction': 0.05, 'Press': 0.075},
                'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
                'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    else:
        fits = {
            'Wood': {'Clearance': -0.05, 'Friction': 0.04, 'Press': 0.05},
            'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
            'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    joint_length = min(lengtha, lengthb)
    cut_length = joint_length / 2
    # thickness = parameters.thickness
    alignment = joint['joint_parameters']['joint_align']
    fit = fits[parameters['material']][joint['joint_parameters']['fit']]

    cuta = f""

    cuta += f"M {0} {0} " + \
            f"L {0} {thickness-fit}" + \
            f"L {cut_length} {thickness-fit}" + \
            f"L {cut_length} {0} Z "

    cutb = f"M {0} {0} " + \
           f"L {0} {thickness-fit}" + \
           f"L {cut_length} {thickness-fit}" + \
           f"L {cut_length} {0} Z "
    cuta = align_joint(cuta, lengtha, thickness, alignment)
    cutb = align_joint(cutb, lengthb, thickness, alignment)

    cuts[facea] = [place_new_edge_path(cuta, patha)]
    cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_joint_adds(joint, model, parameters):
    """process a single joint"""
    jointtype = joint['joint_parameters']['joint_type']
    addfunc = {'Box': get_box_joint_adds,
               'Tab-and-Slot': get_tabslot_joint_adds,
               'Interlocking': get_interlock_joint_adds,
               'Bolt': get_bolt_joint_adds,
               'TSlot': get_tslot_joint_adds,
               'Slotted': get_slotted_joint_adds}
    adds = addfunc.get(jointtype, lambda j, m, c: {})(joint, model, parameters)
    return adds


def get_joint_cuts(joint, model, parameters):
    """process a single joint"""
    jointtype = joint['joint_parameters']['joint_type']
    cutfunc = {'Box': get_box_joint_cuts,
               'Tab-and-Slot': get_tabslot_joint_cuts,
               'Interlocking': get_interlock_joint_cuts,
               'Bolt': get_bolt_joint_cuts,
               'Divider': get_divider_joint_cuts,
               'Flat': get_flat_joint_cuts,
               'TSlot': get_tslot_joint_cuts,
               'Slotted': get_slotted_joint_cuts}
    cuts = cutfunc.get(jointtype, lambda j, m, c: {})(joint, model, parameters)
    return cuts


def get_divider_joint_cuts(joint, _, parameters):
    """generator for divider joints"""
    cuts = {}
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    if thickness > 4.5:
        fits = {'Wood': {'Clearance': -0.05, 'Friction': 0.05, 'Press': 0.075},
                'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
                'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    else:
        fits = {
            'Wood': {'Clearance': -0.05, 'Friction': 0.04, 'Press': 0.05},
            'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
            'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    patha = joint['edge_a']['d']
    pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    lengthb = get_length(pathb)
    joint_length = min(lengtha, lengthb)
    cut_length = joint_length / 2
    # thickness = parameters.thickness
    alignment = joint['joint_parameters']['joint_align']
    fit = fits[parameters['material']][joint['joint_parameters']['fit']]

    cuta = f""

    cuta += f"M {0} {-(thickness-fit)} " + \
            f"L {0} {thickness-fit}" + \
            f"L {cut_length} {thickness-fit}" + \
            f"L {cut_length} {-(thickness-fit)} Z "

    cutb = f"M {0} {-(thickness-fit)/2} " + \
           f"L {0} {(thickness-fit)/2}" + \
           f"L {cut_length} {(thickness-fit)/2}" + \
           f"L {cut_length} {-(thickness-fit)/2} Z "
    # cuta = align_joint(cuta, lengtha, thickness, alignment)
    # cutb = align_joint(cutb, lengthb, thickness, alignment)

    cuts[facea] = [place_new_edge_path(cuta, patha)]
    cuts[faceb] = [place_new_edge_path(cutb, pathb)]

    return cuts


def get_flat_joint_cuts(joint, _, parameters):
    """generator for flat joints"""
    cuts = {}
    cuts = {}
    angle = joint['joint_parameters']['angle']
    thickness = parameters.thickness
    if angle < math.pi / 2:
        thickness = thickness * math.tan(math.pi / 2 - angle) + thickness / math.cos(math.pi / 2 - angle)
    else:
        thickness = thickness * math.sin(angle)
    if thickness > 4.5:
        fits = {'Wood': {'Clearance': -0.05, 'Friction': 0.05, 'Press': 0.075},
                'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
                'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    else:
        fits = {
            'Wood': {'Clearance': -0.05, 'Friction': 0.04, 'Press': 0.05},
            'None': {'Clearance': 0.0, 'Friction': 0.0, 'Press': 0.0},
            'Acrylic': {'Clearance': -0.1, 'Friction': 0.0, 'Press': 0.0}}
    patha = joint['edge_a']['d']
    # pathb = joint['edge_b']['d']
    facea = joint['edge_a']['face']
    # faceb = joint['edge_b']['face']
    lengtha = get_length(patha)
    # lengthb = get_length(pathb)
    # thickness = parameters.thickness
    alignment = joint['joint_parameters']['joint_align']
    fit = fits[parameters['material']]['Clearance']

    # cuta = f""

    cuta = f"M {0} {-(thickness-fit)} " + \
           f"L {0} {thickness-fit}" + \
           f"L {lengtha} {thickness-fit}" + \
           f"L {lengtha} {-(thickness-fit)} Z "
    # cuta = align_joint(cuta, lengtha, thickness, alignment)
    # cutb = align_joint(cutb, lengthb, thickness, alignment)

    cuts[facea] = [place_new_edge_path(cuta, patha)]
    print(cuts[facea])
    # cuts[faceb] = [place_new_edge_path(cutb, pathb)]
    return cuts


def align_joint(path, length, thickness, alignment):
    """returns joint offset inside, middle, or balanced"""
    new_path = path
    alignment_path = {
        'Inside': f"M {0} {0} L {length} {0}",
        'Middle': f"M {0} {-thickness/2.0} L {length} {-thickness/2.0}",
        'Outside': f"M {0} {-thickness} L {length} {-thickness}", }
    new_path = place_new_edge_path(path, alignment_path[alignment])
    return new_path


def kerf_offset(model, parameters):
    """Applies a kerf offset based upon material and laser parameters"""
    kerf_size = parameters.kerf
    original_tree = model['tree']
    tree = {}
    for face, shapes in original_tree.items():
        if face.startswith('face'):
            original = shapes['paths']
            kerf_path = get_kerf(original, kerf_size)
            tree[face] = {
                'paths': kerf_path}

    return tree


def get_processed_model(model, parameters):
    """returns model containing paths of target geometry for each face"""

    output_model = make_blank_model(model['attrib'])
    # output_model['attrib'] = model['attrib']

    original_model = get_original_model(model)
    processed_model = process_joints(
        original_model, model['joints'], parameters)
    output_model['tree'] = kerf_offset(processed_model, parameters)

    return output_model


def get_kerf(paths, kerf_size):
    """calculate kerf compensated path using PyClipper"""
    # PyClipper understands loops not paths
    loops = paths_to_loops(paths)
    kerf_loops = get_offset_loop(loops, kerf_size)
    # change back into paths for output
    kerf_paths = loops_to_paths(kerf_loops)
    return kerf_paths


def get_outside_kerf(tree, parameters):
    """calculate kerf compensated path for visible surfaces"""
    slow_kerf_size = parameters['slow_kerf']
    visible_style = f"fill:none;stroke:#ff0000;stroke-linejoin:round;" + \
        f"stroke-width:{slow_kerf_size}px;stroke-linecap:round;stroke-opacity:0.5"

    for face, shapes in tree.items():
        if face.startswith('Face'):
            original = shapes['Original']['paths']
            processed = shapes['Processed']['paths']
            original_kerf = get_kerf(original, parameters['slow_kerf'])
            processed_kerf = get_kerf(processed, parameters['slow_kerf'])
            outside_kerf = get_overlapping(processed_kerf, original_kerf)
            # outside_kerf = intersect_paths(processed_kerf, original_kerf)
            tree[face]['Visible'] = {
                'paths': outside_kerf,
                'style': visible_style}
    return tree


def get_inside_kerf(tree, parameters):
    """calculate kerf compensated path for non-visible surfaces"""
    fast_kerf_size = parameters['fast_kerf']
    inside_style = f"fill:none;stroke:#0000ff;stroke-linejoin:round;" + \
        f"stroke-width:{fast_kerf_size}px;stroke-linecap:round;stroke-opacity:0.5"

    for face, shapes in tree.items():
        if face.startswith('Face'):
            original = shapes['Original']['paths']
            processed = shapes['Processed']['paths']
            original_kerf = get_kerf(original, parameters['fast_kerf'])
            processed_kerf = get_kerf(processed, parameters['fast_kerf'])
            # inside_kerf = subtract_paths(processed_kerf, original_kerf)
            inside_kerf = get_not_overlapping(processed_kerf, original_kerf)
            tree[face]['Hidden'] = {
                'paths': inside_kerf,
                'style': inside_style}
    return tree


def scale_viewbox(viewbox, scale):
    """scale viewbox(string of 4 numbers) by scale factor (float)"""
    new_viewbox = ""
    for coord in viewbox.split():
        new_viewbox += f"{float(coord) * scale} "
    # print(viewbox.split())
    new_viewbox = new_viewbox.strip()
    return new_viewbox


def scale_tree(tree, scale):
    """scale model"""
    new_tree = tree
    for face, shapes in tree.items():
        if face.startswith('face'):
            for layer, contents in shapes.items():
                scaled_paths = []
                for path in contents['paths']:
                    scaled_path = scale_path(path, scale)
                    scaled_paths.append(scaled_path)
                new_tree[face][layer]['paths'] = scaled_paths
    return new_tree


def scale_joint_params(params, scale):
    """modify scalable joint parameters"""
    new_params = params
    new_params['tabsize'] = params['tabsize'] * scale
    new_params['tabspace'] = params['tabspace'] * scale
    new_params['boltspace'] = params['boltspace'] * scale
    return params


def scale_joints(joints, scale):
    """scale joints(edges and parameters) by scale factor (float)"""
    scaled_joints = joints
    for joint, specs in joints.items():
        scaled_joints[joint]['edge_a']['d'] = scale_path(
            specs['edge_a']['d'], scale)
        scaled_joints[joint]['edge_b']['d'] = scale_path(
            specs['edge_b']['d'], scale)
        scaled_joints[joint]['joint_parameters'] = scale_joint_params(
            specs['joint_parameters'], scale)
    return scaled_joints


def scale_design(design_model, scale):
    """scales design by factor(float)"""
    scaled_model = design_model
    scaled_model['attrib']['viewBox'] = scale_viewbox(
        design_model['attrib']['viewBox'], scale)
    scaled_model['tree'] = scale_tree(design_model['tree'], scale)
    scaled_model['joints'] = scale_joints(design_model['joints'], scale)
    return scaled_model


def process_web_outputsvg(design_model, parameters):
    """process joints and offset kerf"""
    # scaling
    scaled_model = scale_design(design_model, parameters.scale)
    # Processing:
    output_model = get_processed_model(scaled_model, parameters)
    # Styling:
    output_model['attrib']['style'] = f"fill:none;stroke:#ff0000;stroke-linejoin:round;" + \
        f"stroke-width:0.1px;stroke-linecap:round;stroke-opacity:0.5;" + parameters.style

    return output_model


def get_original_model(input_model):
    """process simple kerf offset"""
    output_model = make_blank_model(input_model['attrib'])
    # output_model['attrib'] = input_model['attrib']
    output_model['attrib']['style'] = "fill:#00ff00;fill-opacity:0.25;stroke:none"
    output_model['tree'] = get_original_tree(input_model)
    return output_model


def svg_to_model(filename):
    """converts svg file to design model"""
    model = extract_embeded_model(filename)
    if model is None:
        model = model_from_raw_svg(filename)
    return model


def extract_embeded_model(filename):
    """extracts embeded model if there is one in metadata"""
    model = None
    tree = ET.parse(filename)
    root = tree.getroot()

    for metadata in root.findall('{http://www.w3.org/2000/svg}metadata'):
        lasermetadata = metadata.find(
            '{http://www.w3.org/2000/svg}laserassistant')
        if lasermetadata is not None:
            model = json.loads(lasermetadata.attrib['model'])
    return model


def model_from_raw_svg(filename):
    """creates a new model from a raw svg file without metadata"""
    svg_data = parse_svgfile(
        filename)

    combined_path = svg_to_combined_paths(filename)
    closed_paths, open_paths = separate_closed_paths([combined_path])
    model = paths_to_faces(closed_paths)
    model['attrib'] = svg_data['attrib']
    if open_paths != []:
        model['tree']['Open Paths'] = {'paths': open_paths}
    model['joints'] = {}
    model['edge_data'] = get_edges(model)
    model['joint_index'] = 1
    return model


def get_edges(model):
    """separates perimeter into individual segments"""
    edge_data = {}
    edge_data['viewBox'] = model['attrib']['viewBox']
    edge_data['edges'] = []
    edge_count = 0
    tree = model['tree']
    for face, shapes in tree.items():
        if face.startswith('face'):
            perimeter = shapes['Perimeter']['paths'][0]
            segments = path_to_segments(perimeter)
            for segment in segments:
                edge = {}
                edge['d'] = segment
                edge['face'] = face
                edge_count = edge_count + 1
                edge['edge'] = edge_count
                edge_data['edges'].append(edge)
    return edge_data


def add_joints(model):
    """adds joints to tree"""
    tree = model['tree']
    joints = model['joints']
    for joint in joints:
        new_edge = {"paths": [joints[joint]['path']]}
        if 'Joints' not in tree[joints[joint]['face']]:
            tree[joints[joint]['face']]['Joints'] = {}
        tree[joints[joint]['face']]['Joints'][joint] = new_edge
    model['tree'] = tree
    return model


def svg_to_combined_paths(filename):
    """converts svg file to a single combined path"""
    paths, _ = SVGPT.svg2paths(filename)
    combined_path = SVGPT.concatpaths(paths)
    return combined_path.d()


def paths_to_faces(paths):
    """takes a list of paths and returns model with faces"""
    model = make_blank_model()
    perims, cuts = separate_perims_from_cuts(paths)

    for index, perim in enumerate(perims):
        model['tree'][f"face{index+1}"] = {
            "Perimeter": {'paths': [perim]}, "Cuts": {'paths': []}}
        for cut in cuts:
            if is_inside(cut, perim):
                model['tree'][f"face{index+1}"]['Cuts']['paths'].append(cut)

    return model


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process metaSVG for printing.')
    parser.add_argument('--preset', help='preset to load (read from \'presets.csv\'); other arguments, if given, will override preset')

    parser.add_argument('--thickness', help='material thickness (mm)', type=float)
    parser.add_argument('--width', help='material width (mm)', type=float)
    parser.add_argument('--height', help='material height (mm)', type=float)
    parser.add_argument('--kerf', help='cut width (mm)', type=float)
    parser.add_argument('--boxC', help='adjustment for clearance fit in box joint (mm)', type=float)
    parser.add_argument('--boxL', help='adjustment for friction fit in box joint (mm)', type=float)
    parser.add_argument('--boxI', help='adjustment for press fit in box joint (mm)', type=float)
    parser.add_argument('--tabC', help='adjustment for clearance fit in tab-and-slot joint (mm)', type=float)
    parser.add_argument('--tabL', help='adjustment for friction fit in tab-and-slot joint (mm)', type=float)
    parser.add_argument('--tabI', help='adjustment for press fit in tab-and-slot joint (mm)', type=float)
    parser.add_argument('--slotC', help='adjustment for clearance fit in slotted joint (mm)', type=float)
    parser.add_argument('--slotL', help='adjustment for friction fit in slotted joint (mm)', type=float)
    parser.add_argument('--slotI', help='adjustment for press fit in slotted joint (mm)', type=float)
    parser.add_argument('--style', help='added style for output svg', type=str)
    parser.add_argument('--notes', help='notes for preset [probably not used!]', type=str)

    parser.add_argument('--scale', help='scale factor (default: 1.0)', type=float)
    parser.add_argument('metasvg', help='metaSVG file to process')
    parser.add_argument('outsvg', help='svg to output')
    args = parser.parse_args()

    print(f"Loading model from '{args.metasvg}'...")
    model = svg_to_model(args.metasvg)
    print(f"  Loaded model with {len(model['joints'])} joints")

    #default params, replaced by --preset or modified by other --args
    params = {
        'thickness':0.0,
        'width':0.0,
        'height':0.0,
        'kerf':0.0,
        'boxC':0.0, 'boxL':0.0, 'boxI':0.0,
        'tabC':0.0, 'tabL':0.0, 'tabI':0.0,
        'slotC':0.0, 'slotL':0.0, 'slotI':0.0,
        'style':'stroke:#000000;stroke-width:1px;',
        'preset':None,
        'notes':'',
        'scale':1.0
    }

    if args.preset != None:
        print(f"Looking for preset '{args.preset}' from 'presets.csv'...")
        names = []
        with open('presets.csv', 'r') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if 'preset' not in row:
                    print("  Skipping row without 'preset' column.")
                    continue
                try:
                    lp = LaserParameters(row) #see if row can be parsed into LaserParameters
                except:
                    print(f"  Skipping row '{row.preset}' that doesn't parse into LaserParameters")
                    continue
                names.append(row['preset'])
                if row['preset'] == args.preset:
                    params = row
                    break
        if params['preset'] == args.preset:
            print(f"  Found preset '{args.preset}'.")
        else:
            print(f"  Failed to find preset '{args.preset}' in 'presets.csv'.\n  Available presets: {', '.join(names)}")
            sys.exit(1)

    for name in [
        'scale',
        'width',
        'height',
        'kerf',
        'boxC', 'boxL', 'boxI',
        'tabC', 'tabL', 'tabI',
        'slotC', 'slotL', 'slotI',
        'style',
        'notes']:
        if getattr(args, name) != None:
            print(f"Setting {name} to {getattr(args, name)} .")
            params[name] = getattr(args, name)

    print(f"Converting presets/arguments to LaserParameters...")
    params = LaserParameters(params)
    print(f"  done.")

    print(f"Using parameters:")
    for name in dir(params):
        if name.startswith("__"): continue
        if name == "FIT_MAP": continue
        if name == "get_fit": continue
        print(f"  parameters.{name} = {repr(getattr(params, name))}")

    print(f"Processing model...")
    new_model = process_web_outputsvg(model, params)
    print(f"Writing model to '{args.outsvg}'...")
    model_to_svg_file(new_model, design=model, filename=args.outsvg)
    print(f"  done.")


