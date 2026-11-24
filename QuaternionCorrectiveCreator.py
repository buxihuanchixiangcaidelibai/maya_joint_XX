# -*- coding: utf-8 -*-
"""Maya 2025 四元数修型骨骼创建工具。

Usage in Maya's Python tab:
    import sys
    sys.path.insert(0, r"C:/path/to/Maya_Rig_Python/quaternion_corrective_tools")
    import QuaternionCorrectiveCreator as qcc
    qcc.show()

选择一个或多个源关节，设置参数后点击“为选中关节创建”。
工具只创建驱动与蒙皮修型关节，不修改 skinCluster 影响对象或权重。
"""
from __future__ import absolute_import, division, print_function

import re

import maya.cmds as cmds
import maya.api.OpenMaya as om
import maya.OpenMayaUI as omui

from PySide6 import QtCore, QtWidgets
from shiboken6 import wrapInstance


WINDOW = "QuaternionCorrectiveCreatorWindow"
DRIVER_DISPLAY_LAYER = "QuaternionCorrectiveDrivers_LYR"
SKIN_DISPLAY_LAYER = "QuaternionCorrectiveSkin_LYR"
CORRECTIVE_RADIUS_OFFSET = 0.5
MANUAL_DRIVE_VERSION = 3
DRIVER_CHANNEL_GROUPS = (
    ("旋转", ("rotateX", "rotateY", "rotateZ")),
    ("位移", ("translateX", "translateY", "translateZ")),
    ("缩放", ("scaleX", "scaleY", "scaleZ")),
)
DRIVER_CHANNEL_RE = re.compile(r"^(?:translate|rotate|scale)[XYZ]$")

CORRECTIVE_VALUE_RE = re.compile(
    r"^(?:quatCorrectiveWeight|quatCorrectiveRotationMoveMultiplier|"
    r"qc(?:Pos|Neg)[XYZ]Move(?:[XYZ])?Multiplier|"
    r"qcManual_.+Move(?:[XYZ])?Multiplier)$")


def _corrective_value_attrs(node):
    """Returns artist-facing corrective numeric attributes on a scene node."""
    attributes = cmds.listAttr(node, userDefined=True) or []
    return sorted(attribute for attribute in attributes
                  if CORRECTIVE_VALUE_RE.match(attribute))


def _connected_multiplier_info(node, attribute):
    """Returns the actual driven bone and movement axis for a multiplier."""
    automatic = re.match(
        r"^qc(?:Pos|Neg)([XYZ])Move(?:([XYZ]))?Multiplier$", attribute)
    manual = re.match(r"^qcManual_(.+)Move(?:([XYZ]))?Multiplier$", attribute)
    move_axis = None
    if automatic:
        move_axis = automatic.group(2) or automatic.group(1)
    elif manual and manual.group(2):
        move_axis = manual.group(2)
    try:
        queue = list(cmds.listConnections(
            "{}.{}".format(node, attribute),
            source=False, destination=True) or [])
    except (AttributeError, RuntimeError):
        return None, move_axis
    visited = set()
    target = None
    for _depth in range(7):
        next_queue = []
        for candidate in queue:
            if candidate in visited or not cmds.objExists(candidate):
                continue
            visited.add(candidate)
            candidate_name = _short(candidate)
            if not move_axis:
                for pattern in (
                        r"_([XYZ])OwnAxisMoveVector(?:\d+)?$",
                        r"_Translate([XYZ])Sum(?:\d+)?$",
                        r"_Manual(?:Pos|Neg)?([XYZ])_"):
                    axis_match = re.search(pattern, candidate_name)
                    if axis_match:
                        move_axis = axis_match.group(1)
                        break
            if (cmds.attributeQuery(
                    "isQuaternionSurfaceDriver",
                    node=candidate, exists=True) or
                    cmds.attributeQuery(
                        "isQuaternionManualMoveTarget",
                        node=candidate, exists=True)):
                target = candidate
                if move_axis:
                    return target, move_axis
            next_queue.extend(cmds.listConnections(
                candidate, source=False, destination=True) or [])
        queue = next_queue
        if not queue:
            break
    if target and not move_axis:
        move_axis = (_recorded_primary_move_axis(target) or
                     ((_recorded_move_axes(target) or (None,))[0]))
    return target, move_axis


def _friendly_input_channel(channel):
    """Returns an artist-facing transform-channel name."""
    match = re.match(r"^(rotate|translate|scale)([XYZ])$", channel or "")
    if not match:
        return channel or "未知输入"
    kind = {
        "rotate": "旋转",
        "translate": "输入位移",
        "scale": "缩放",
    }[match.group(1)]
    return "{} {}".format(kind, match.group(2))


def _friendly_driver_attr(attribute, target_driver=None, move_axis=None,
                          input_plug=None, source_node=None):
    """Converts one numeric branch into a short artist-facing UI label."""
    if attribute == "quatCorrectiveWeight":
        return "父级四元数旋转权重"
    if attribute == "quatCorrectiveRotationMoveMultiplier":
        return "整体旋转位移倍率"

    if target_driver and move_axis:
        if input_plug and "." in input_plug:
            input_node, input_channel = input_plug.rsplit(".", 1)
            prefix = ""
            if (source_node and
                    _short(input_node) != _short(source_node)):
                prefix = "{}：".format(_short(input_node))
            return "{}{} → 位移 {}".format(
                prefix, _friendly_input_channel(input_channel), move_axis)
        return "位移 {}".format(move_axis)

    automatic = re.match(
        r"^qc(Pos|Neg)([XYZ])Move(?:([XYZ]))?Multiplier$", attribute)
    if automatic:
        side = "正" if automatic.group(1) == "Pos" else "负"
        surface_axis = automatic.group(2)
        target_name = "{} {} 修型骨骼".format(side, surface_axis)
        axis = move_axis or automatic.group(3) or surface_axis
        return "{} · 位移 {}".format(target_name, axis)

    manual = re.match(r"^qcManual_(.+)Move(?:([XYZ]))?Multiplier$", attribute)
    if manual:
        target_name = manual.group(1)
        axis = move_axis or manual.group(2)
        return ("{} · 位移 {}".format(target_name, axis)
                if axis else target_name)
    return attribute


def _friendly_driver_row(attribute, target_driver=None, move_axis=None,
                         input_plug=None, source_node=None):
    """Builds one flat row containing both the bone name and drive branch."""
    branch = _friendly_driver_attr(
        attribute, target_driver, move_axis, input_plug, source_node)
    if target_driver and move_axis:
        return "{}  |  {}".format(_short(target_driver), branch)
    return "系统设置  |  {}".format(branch)


def _connect(source, destination):
    if not cmds.isConnected(source, destination):
        cmds.connectAttr(source, destination, force=True)


def _short(node):
    return node.rsplit("|", 1)[-1].replace(":", "_")


def _mesh_shape(mesh):
    shapes = cmds.listRelatives(mesh, shapes=True, noIntermediate=True, fullPath=True) or []
    if not shapes or cmds.nodeType(shapes[0]) != "mesh":
        raise RuntimeError("{} 不是多边形网格变换节点。".format(mesh))
    return shapes[0]


def _ray_mesh_intersection(mesh, origin, direction, max_distance):
    """Returns the first mesh hit along one normalized world-space direction."""
    selection = om.MSelectionList()
    selection.add(_mesh_shape(mesh))
    path = selection.getDagPath(0)
    mesh_fn = om.MFnMesh(path)
    ray_source = om.MFloatPoint(*origin)
    ray_direction = om.MFloatVector(direction.x, direction.y, direction.z)
    hit = mesh_fn.closestIntersection(
        ray_source, ray_direction, om.MSpace.kWorld,
        float(max_distance), False)
    if hit is None:
        raise RuntimeError(
            "从关节沿所选自身轴投射 {:.3f} 个单位仍未碰到模型表面。"
            "请检查投射轴或增大最大投射距离。".format(max_distance))
    point = hit[0]
    return point.x, point.y, point.z


def _source_axis_world(source, axis):
    """Returns an axis of the selected source joint itself, expressed in world space.

    Corrective roots and surface joints are deliberately based on the selected
    joint's own local orientation, not on its child's orientation or world axes.
    """
    matrix = om.MMatrix(cmds.xform(source, query=True, worldSpace=True, matrix=True))
    vectors = {"X": om.MVector(1, 0, 0), "Y": om.MVector(0, 1, 0), "Z": om.MVector(0, 0, 1)}
    result = vectors[axis] * matrix
    if result.length() < 1e-8:
        raise RuntimeError("{} 的 {} 本地轴无效。".format(source, axis))
    result.normalize()
    return result


def _node_axis_in_parent_space(node, axis):
    """Captures a unit move along ``node``'s own axis in Translate space.

    A plain inverse parent matrix is insufficient for modern Maya rigs:
    offsetParentMatrix, inheritsTransform, joint scale compensation and shear
    can all make the effective Translate basis differ from the parent's world
    matrix.  Sampling the three Translate channels asks Maya for the actual
    evaluated basis, then solves the channel vector that produces the desired
    unit world-space move.  The original pose is restored before returning.
    """
    world_axis = _source_axis_world(node, axis)
    rest = cmds.getAttr(node + ".translate")[0]
    origin = om.MVector(*cmds.xform(
        node, query=True, worldSpace=True, translation=True))
    translate_basis = []
    try:
        for index in range(3):
            sample = list(rest)
            sample[index] += 1.0
            cmds.setAttr(node + ".translate", *sample, type="double3")
            position = om.MVector(*cmds.xform(
                node, query=True, worldSpace=True, translation=True))
            translate_basis.append(position - origin)
            cmds.setAttr(node + ".translate", *rest, type="double3")
    except RuntimeError:
        raise RuntimeError(
            "{} 的 Translate 无法读取局部轴；请确认位移通道未锁定且没有已有连接。".format(
                _short(node)))
    finally:
        try:
            cmds.setAttr(node + ".translate", *rest, type="double3")
        except RuntimeError:
            pass

    basis_matrix = om.MMatrix((
        translate_basis[0].x, translate_basis[0].y, translate_basis[0].z, 0.0,
        translate_basis[1].x, translate_basis[1].y, translate_basis[1].z, 0.0,
        translate_basis[2].x, translate_basis[2].y, translate_basis[2].z, 0.0,
        0.0, 0.0, 0.0, 1.0))
    try:
        result = world_axis * basis_matrix.inverse()
    except RuntimeError:
        raise RuntimeError("{} 的 Translate 轴矩阵不可逆。".format(_short(node)))
    if result.length() < 1e-8:
        raise RuntimeError("{} 的 {} 自身轴无法转换到 Translate 空间。".format(node, axis))
    return result.x, result.y, result.z


def _ensure_attr(node, name, default, minimum=None, maximum=None, keyable=True):
    if not cmds.attributeQuery(name, node=node, exists=True):
        kwargs = {"longName": name, "attributeType": "double", "defaultValue": default, "keyable": keyable}
        if minimum is not None:
            kwargs["minValue"] = minimum
        if maximum is not None:
            kwargs["maxValue"] = maximum
        cmds.addAttr(node, **kwargs)
    return "{}.{}".format(node, name)


def _set_locked_axis_attr(node, name, value, attribute_type, **kwargs):
    """Creates or updates one hidden rig-metadata attribute."""
    plug = "{}.{}".format(node, name)
    if not cmds.attributeQuery(name, node=node, exists=True):
        options = {
            "longName": name,
            "attributeType": attribute_type,
            "keyable": False,
        }
        options.update(kwargs)
        cmds.addAttr(node, **options)
    else:
        cmds.setAttr(plug, lock=False)
    cmds.setAttr(plug, value)
    cmds.setAttr(plug, lock=True)
    return plug


def _record_axis_metadata(node, rotation_axis, primary_axis, move_axes):
    """Stores intended rotation and movement axes independently of position."""
    axes = "XYZ"
    if primary_axis not in axes:
        raise RuntimeError("{} 不是有效的主移动轴。".format(primary_axis))
    requested = set(axis for axis in move_axes if axis in axes)
    requested.add(primary_axis)
    if rotation_axis in axes:
        _set_locked_axis_attr(
            node, "qcRotationAxis", axes.index(rotation_axis), "enum",
            enumName="X:Y:Z")
    _set_locked_axis_attr(
        node, "qcPrimaryMoveAxis", axes.index(primary_axis), "enum",
        enumName="X:Y:Z")
    for axis in axes:
        _set_locked_axis_attr(
            node, "qcMove" + axis, axis in requested, "bool")


def _recorded_rotation_axis(node):
    if not cmds.attributeQuery("qcRotationAxis", node=node, exists=True):
        return None
    index = cmds.getAttr(node + ".qcRotationAxis")
    return "XYZ"[index] if index in (0, 1, 2) else None


def _recorded_primary_move_axis(node):
    if not cmds.attributeQuery("qcPrimaryMoveAxis", node=node, exists=True):
        return None
    index = cmds.getAttr(node + ".qcPrimaryMoveAxis")
    return "XYZ"[index] if index in (0, 1, 2) else None


def _recorded_move_axes(node):
    """Returns explicit movement-axis metadata, or empty for an older rig."""
    attributes = ["qcMove" + axis for axis in "XYZ"]
    if not all(cmds.attributeQuery(
            attribute, node=node, exists=True) for attribute in attributes):
        return ()
    return tuple(axis for axis, attribute in zip("XYZ", attributes)
                 if cmds.getAttr("{}.{}".format(node, attribute)))


def _set_locked_string_attr(node, name, value):
    plug = "{}.{}".format(node, name)
    if not cmds.attributeQuery(name, node=node, exists=True):
        cmds.addAttr(node, longName=name, dataType="string", keyable=False)
    else:
        cmds.setAttr(plug, lock=False)
    cmds.setAttr(plug, value or "", type="string")
    cmds.setAttr(plug, lock=True)
    return plug


def _record_corrective_root_source(root, source):
    """Stores the source joint on its corrective root with a rename-safe link."""
    attribute = "qcSourceJoint"
    plug = "{}.{}".format(root, attribute)
    if not cmds.attributeQuery(attribute, node=root, exists=True):
        cmds.addAttr(root, longName=attribute, attributeType="message")
    current = cmds.listConnections(
        plug, source=True, destination=False, plugs=True) or []
    source_long = (cmds.ls(source, long=True) or [source])[0]
    source_plug = source_long + ".message"
    for connection in current:
        if connection != source_plug and cmds.isConnected(connection, plug):
            cmds.disconnectAttr(connection, plug)
    _connect(source_plug, plug)


def _recorded_corrective_root_source(root):
    """Returns the source joint explicitly recorded on a corrective root."""
    if not cmds.attributeQuery("qcSourceJoint", node=root, exists=True):
        return None
    sources = cmds.listConnections(
        root + ".qcSourceJoint", source=True, destination=False,
        type="joint") or []
    if not sources:
        return None
    return (cmds.ls(sources[0], long=True) or [sources[0]])[0]


def _same_node(first, second):
    """Compares two Maya nodes without depending on short names."""
    if not first or not second:
        return False
    first_long = (cmds.ls(first, long=True) or [first])[0]
    second_long = (cmds.ls(second, long=True) or [second])[0]
    return first_long == second_long


def _legacy_corrective_root_matches_source(root, source):
    """Recognizes an older root, then lets the caller migrate its metadata."""
    if cmds.attributeQuery("qcQuaternionSource", node=root, exists=True):
        recorded_path = cmds.getAttr(root + ".qcQuaternionSource") or ""
        if recorded_path and cmds.objExists(recorded_path):
            if _same_node(recorded_path, source):
                return True

    constraints = cmds.listConnections(
        root, source=True, destination=False, type="pointConstraint") or []
    for constraint in constraints:
        for target in cmds.pointConstraint(
                constraint, query=True, targetList=True) or []:
            if _same_node(target, source):
                return True
    return False


def _corrective_root_for_source(source):
    """Finds a source joint's driver root using metadata, with legacy migration."""
    if not cmds.objExists(source) or cmds.nodeType(source) != "joint":
        raise RuntimeError("{} 不是有效的源关节。".format(source))

    candidates = []
    connected = cmds.listConnections(
        source + ".message", source=False, destination=True,
        type="joint") or []
    for node in connected:
        if (cmds.attributeQuery(
                "isQuaternionCorrectiveRoot", node=node, exists=True) and
                _same_node(_recorded_corrective_root_source(node), source)):
            candidates.append(node)

    if not candidates:
        roots = cmds.ls(
            "*.isQuaternionCorrectiveRoot", objectsOnly=True, long=True) or []
        for root in roots:
            if _legacy_corrective_root_matches_source(root, source):
                candidates.append(root)

    unique = []
    for node in candidates:
        long_name = (cmds.ls(node, long=True) or [node])[0]
        if long_name not in unique:
            unique.append(long_name)
    if not unique:
        raise RuntimeError(
            "找不到 {} 对应的驱动根骨骼。请先在“创建”页为该关节创建修型系统。".format(
                _short(source)))
    if len(unique) > 1:
        raise RuntimeError(
            "{} 对应到多个驱动根骨骼，请先清理重复的修型系统：{}。".format(
                _short(source), "、".join(_short(node) for node in unique)))
    root = unique[0]
    _record_corrective_root_source(root, source)
    return root


def _manual_driver_parents_for_root(root):
    """Returns stable manual-driver groups associated with one QC root."""
    parents = cmds.listRelatives(root, parent=True, fullPath=True) or []
    if not parents:
        return []
    candidates = cmds.listRelatives(
        parents[0], children=True, type="transform", fullPath=True) or []
    result = []
    for candidate in candidates:
        if not cmds.attributeQuery(
                "isQuaternionManualDriverRoot",
                node=candidate, exists=True):
            continue
        if not cmds.attributeQuery(
                "qcCorrectiveRoot", node=candidate, exists=True):
            continue
        connected = cmds.listConnections(
            candidate + ".qcCorrectiveRoot",
            source=True, destination=False) or []
        if connected and _same_node(connected[0], root):
            result.append(candidate)
    return result


def _manual_driver_parent_for_source(source, root, follow_node=None):
    """Gets a stable manual parent for one rotation-source/follow-space pair."""
    follow_node = follow_node or source
    if not cmds.objExists(follow_node):
        raise RuntimeError("空间跟随对象不存在：{}。".format(follow_node))

    matching = []
    for group in _manual_driver_parents_for_root(root):
        recorded_follow = []
        if cmds.attributeQuery("qcFollowNode", node=group, exists=True):
            recorded_follow = cmds.listConnections(
                group + ".qcFollowNode",
                source=True, destination=False) or []
        if not recorded_follow:
            # Compatibility with stable groups created before qcFollowNode:
            # their orient constraint target is their spatial follow object.
            for constraint in cmds.listConnections(
                    group, source=True, destination=False,
                    type="orientConstraint") or []:
                recorded_follow.extend(cmds.orientConstraint(
                    constraint, query=True, targetList=True) or [])
        if recorded_follow and _same_node(recorded_follow[0], follow_node):
            if not cmds.attributeQuery("qcFollowNode", node=group, exists=True):
                cmds.addAttr(
                    group, longName="qcFollowNode", attributeType="message")
                _connect(follow_node + ".message", group + ".qcFollowNode")
            matching.append(group)

    if len(matching) > 1:
        raise RuntimeError(
            "{} 在 {} 空间下存在多个新增驱动稳定父级：{}。".format(
                _short(source), _short(follow_node),
                "、".join(_short(node) for node in matching)))
    if matching:
        group = matching[0]
        constraint_commands = (
            ("pointConstraint", cmds.pointConstraint),
            ("orientConstraint", cmds.orientConstraint),
            ("scaleConstraint", cmds.scaleConstraint),
        )
        for constraint_type, command in constraint_commands:
            constraints = cmds.listConnections(
                group, source=True, destination=False,
                type=constraint_type) or []
            valid = any(
                any(_same_node(target, follow_node) for target in
                    (command(
                        constraint, query=True, targetList=True) or []))
                for constraint in constraints)
            if constraints and not valid:
                raise RuntimeError(
                    "{} 已有其他{}，无法安全跟随 {}。".format(
                        _short(group), constraint_type,
                        _short(follow_node)))
            if not constraints:
                command(
                    follow_node, group,
                    maintainOffset=not _same_node(follow_node, source),
                    name="{}_QC_{}_ManualRoot_{}".format(
                        _short(source), _short(follow_node),
                        constraint_type))
        return group

    root_parents = cmds.listRelatives(
        root, parent=True, fullPath=True) or []
    if not root_parents:
        raise RuntimeError(
            "{} 没有四元数参考组，无法创建新增驱动稳定父级。".format(
                _short(root)))
    buffer = root_parents[0]
    source_space = _same_node(follow_node, source)
    name = (_short(source) + "_QC_MANUAL_DRV_GRP"
            if source_space else
            "{}_QC_{}_SPACE_MANUAL_DRV_GRP".format(
                _short(source), _short(follow_node)))
    group = cmds.createNode("transform", name=name, parent=buffer)
    cmds.setAttr(group + ".rotate", 0.0, 0.0, 0.0, type="double3")
    cmds.addAttr(
        group, longName="isQuaternionManualDriverRoot",
        attributeType="bool", defaultValue=True)
    cmds.setAttr(group + ".isQuaternionManualDriverRoot", lock=True)
    cmds.addAttr(group, longName="qcCorrectiveRoot", attributeType="message")
    _connect(root + ".message", group + ".qcCorrectiveRoot")
    cmds.addAttr(group, longName="qcSourceJoint", attributeType="message")
    _connect(source + ".message", group + ".qcSourceJoint")
    cmds.addAttr(group, longName="qcFollowNode", attributeType="message")
    _connect(follow_node + ".message", group + ".qcFollowNode")
    cmds.pointConstraint(
        follow_node, group, maintainOffset=not source_space,
        name="{}_QC_{}_ManualRoot_pointConstraint".format(
            _short(source), _short(follow_node)))
    cmds.orientConstraint(
        follow_node, group, maintainOffset=not source_space,
        name="{}_QC_{}_ManualRoot_orientConstraint".format(
            _short(source), _short(follow_node)))
    cmds.scaleConstraint(
        follow_node, group, maintainOffset=not source_space,
        name="{}_QC_{}_ManualRoot_scaleConstraint".format(
            _short(source), _short(follow_node)))
    return (cmds.ls(group, long=True) or [group])[0]


def _record_branch_input(driver, move_axis, source_node, source_channel):
    """Stores one movement branch's input object and scalar channel."""
    if move_axis not in "XYZ" or not DRIVER_CHANNEL_RE.match(source_channel):
        raise RuntimeError("无效的驱动通道：{}".format(source_channel))
    if not cmds.objExists(source_node):
        raise RuntimeError("连接对象不存在：{}".format(source_node))
    message_attr = "qcMove{}Source".format(move_axis)
    if not cmds.attributeQuery(message_attr, node=driver, exists=True):
        cmds.addAttr(driver, longName=message_attr, attributeType="message")
    _connect(source_node + ".message", driver + "." + message_attr)
    _set_locked_string_attr(
        driver, "qcMove{}SourceChannel".format(move_axis), source_channel)


def _recorded_branch_input(driver, move_axis):
    message_attr = "qcMove{}Source".format(move_axis)
    channel_attr = "qcMove{}SourceChannel".format(move_axis)
    if not (cmds.attributeQuery(message_attr, node=driver, exists=True) and
            cmds.attributeQuery(channel_attr, node=driver, exists=True)):
        return None
    sources = cmds.listConnections(
        driver + "." + message_attr,
        source=True, destination=False) or []
    channel = cmds.getAttr(driver + "." + channel_attr) or ""
    if not sources or not DRIVER_CHANNEL_RE.match(channel):
        return None
    long_names = cmds.ls(sources[0], long=True) or [sources[0]]
    return "{}.{}".format(long_names[0], channel)


def _branch_reaches_target(start_node, target_driver):
    """Checks whether a computation branch actually ends at one target."""
    if not target_driver or not cmds.objExists(target_driver):
        return False
    target_long = (cmds.ls(target_driver, long=True) or [target_driver])[0]
    allowed_types = {
        "multiplyDivide", "plusMinusAverage", "multDoubleLinear",
        "addDoubleLinear", "unitConversion"}
    queue = [start_node]
    visited = set()
    while queue:
        node = queue.pop(0)
        if node in visited or not cmds.objExists(node):
            continue
        visited.add(node)
        node_long = (cmds.ls(node, long=True) or [node])[0]
        if node_long == target_long:
            return True
        for downstream in cmds.listConnections(
                node, source=False, destination=True) or []:
            downstream_long = (cmds.ls(downstream, long=True) or
                               [downstream])[0]
            if downstream_long == target_long:
                return True
            if (cmds.objExists(downstream) and
                    cmds.nodeType(downstream) in allowed_types):
                queue.append(downstream)
    return False


def _movement_branch_nodes(multiplier_node, attribute,
                           target_driver=None, move_axis=None):
    """Returns the exact distance/angle branch for one target movement axis."""
    plug = "{}.{}".format(multiplier_node, attribute)
    destinations = cmds.listConnections(
        plug, source=False, destination=True, plugs=True) or []
    candidates = []
    for destination in destinations:
        destination_node, destination_attr = destination.rsplit(".", 1)
        if (destination_attr == "input2" and
                cmds.nodeType(destination_node) == "multDoubleLinear"):
            angle_sources = cmds.listConnections(
                destination_node + ".input1",
                source=True, destination=False, plugs=True) or []
            if not angle_sources:
                continue
            angle_node = angle_sources[0].rsplit(".", 1)[0]
            if cmds.nodeType(angle_node) == "multDoubleLinear":
                candidates.append((destination_node, angle_node))
    if target_driver:
        for distance_node, angle_node in candidates:
            vectors = cmds.listConnections(
                distance_node, source=False, destination=True,
                type="multiplyDivide") or []
            for vector in vectors:
                if move_axis and _vector_node_axis(vector) != move_axis:
                    continue
                if _branch_reaches_target(vector, target_driver):
                    return distance_node, angle_node
        # A target was supplied, so silently falling back to another branch
        # would rewire the wrong corrective joint in legacy/duplicated graphs.
        return None, None
    if candidates:
        return candidates[0]
    return None, None


def _upstream_transform_channel(start_node):
    """Finds the actual transform channel feeding a computation branch."""
    allowed_types = {
        "multiplyDivide", "multDoubleLinear", "addDoubleLinear",
        "clamp", "condition", "plusMinusAverage", "unitConversion"}
    queue = [start_node]
    visited = set()
    while queue:
        node = queue.pop(0)
        if node in visited or not cmds.objExists(node):
            continue
        visited.add(node)
        for source_plug in cmds.listConnections(
                node, source=True, destination=False, plugs=True) or []:
            source_node, source_attr = source_plug.rsplit(".", 1)
            if DRIVER_CHANNEL_RE.match(source_attr):
                long_names = cmds.ls(source_node, long=True) or [source_node]
                return "{}.{}".format(long_names[0], source_attr)
            if (cmds.objExists(source_node) and
                    cmds.nodeType(source_node) in allowed_types):
                queue.append(source_node)
    return None


def _movement_branch_input(multiplier_node, attribute,
                           target_driver=None, move_axis=None):
    """Returns the graph's real input plug, falling back to stored metadata."""
    _distance, angle_node = _movement_branch_nodes(
        multiplier_node, attribute, target_driver, move_axis)
    actual = (_upstream_transform_channel(angle_node)
              if angle_node else None)
    if actual:
        return actual
    if target_driver and move_axis:
        return _recorded_branch_input(target_driver, move_axis)
    return None


def _normalize_legacy_branch_neutral(multiplier_node, attribute,
                                     target_driver, move_axis):
    """Repairs current-pose offsets made by the preceding tool version.

    Old channel rewires stored ``-currentValue`` in a tagged addDoubleLinear.
    That made the multiplier move a joint even after its source returned to a
    normal neutral pose.  Only tool-tagged input nodes are changed here.
    """
    if not target_driver or not move_axis:
        return False
    _distance, angle_node = _movement_branch_nodes(
        multiplier_node, attribute, target_driver, move_axis)
    if not angle_node:
        return False
    actual_plug = _upstream_transform_channel(angle_node)
    if not actual_plug:
        return False
    source_channel = actual_plug.rsplit(".", 1)[-1]
    neutral_offset = -1.0 if source_channel.startswith("scale") else 0.0

    queue = [plug.rsplit(".", 1)[0] for plug in cmds.listConnections(
        angle_node + ".input1",
        source=True, destination=False, plugs=True) or []]
    visited = set()
    while queue:
        node = queue.pop(0)
        if node in visited or not cmds.objExists(node):
            continue
        visited.add(node)
        node_type = cmds.nodeType(node)
        if (node_type == "addDoubleLinear" and
                cmds.attributeQuery(
                    "isQuaternionChannelInput", node=node, exists=True)):
            current = float(cmds.getAttr(node + ".input1"))
            if abs(current - neutral_offset) > 1e-8:
                cmds.setAttr(node + ".input1", neutral_offset)
                return True
            return False
        if node_type in {"unitConversion", "addDoubleLinear"}:
            queue.extend(cmds.listConnections(
                node, source=True, destination=False) or [])
    return False


def _delete_orphaned_channel_node(node):
    """Deletes a superseded private input node when no branch still uses it."""
    if not node or not cmds.objExists(node):
        return
    if cmds.listConnections(node, source=False, destination=True):
        return
    tagged = cmds.attributeQuery(
        "isQuaternionChannelInput", node=node, exists=True)
    name = _short(node)
    legacy = ("_QC_" in name and cmds.nodeType(node) in {
        "clamp", "multDoubleLinear", "addDoubleLinear"})
    if not (tagged or legacy):
        return
    upstream = cmds.listConnections(
        node, source=True, destination=False) or []
    cmds.delete(node)
    for candidate in upstream:
        _delete_orphaned_channel_node(candidate)


def rewire_movement_branch(multiplier_node, attribute, target_driver,
                           move_axis, source_node, source_channel):
    """Rewires one multiplier branch to a zero-based transform channel.

    Driver-manager values are gains, not pose controls.  Rotate/translate are
    therefore neutral at 0 and scale is neutral at 1.  Changing a multiplier
    while its source channel is neutral must never move the corrective joint.
    """
    if move_axis not in "XYZ":
        raise RuntimeError("无法识别该项目的移动轴。")
    if not cmds.objExists(source_node):
        raise RuntimeError("连接对象不存在：{}".format(source_node))
    if not DRIVER_CHANNEL_RE.match(source_channel):
        raise RuntimeError("不支持的连接通道：{}".format(source_channel))
    if not cmds.attributeQuery(
            source_channel, node=source_node, exists=True):
        raise RuntimeError(
            "{} 没有 {} 通道。".format(_short(source_node), source_channel))
    _distance, angle_node = _movement_branch_nodes(
        multiplier_node, attribute, target_driver, move_axis)
    if not angle_node:
        raise RuntimeError("找不到该驱动项的角度/距离换算节点。")

    source_plug = "{}.{}".format(source_node, source_channel)
    previous_inputs = cmds.listConnections(
        angle_node + ".input1",
        source=True, destination=False, plugs=True) or []

    input_node = None
    input_plug = source_plug
    if source_channel.startswith("scale"):
        # Maya scale is neutral at 1, so feed (scale - 1) into the movement
        # graph.  Rotate and translate can connect directly because their
        # neutral value is already 0.
        input_node = cmds.createNode(
            "addDoubleLinear",
            name="{}_Move{}_{}_ZeroOffset".format(
                _short(target_driver), move_axis, source_channel))
        cmds.addAttr(
            input_node, longName="isQuaternionChannelInput",
            attributeType="bool", defaultValue=True)
        cmds.setAttr(input_node + ".isQuaternionChannelInput", lock=True)
        cmds.setAttr(input_node + ".input1", -1.0)
        _connect(source_plug, input_node + ".input2")
        input_plug = input_node + ".output"
    _connect(input_plug, angle_node + ".input1")

    # Read the selected branch itself.  Do not use metadata fallback here:
    # this is the final proof that Maya's dependency graph was really rewired.
    actual_plug = _upstream_transform_channel(angle_node)
    if not actual_plug:
        raise RuntimeError("改线后无法确认该驱动项的实际输入。")
    actual_node, actual_channel = actual_plug.rsplit(".", 1)
    source_long = (cmds.ls(source_node, long=True) or [source_node])[0]
    actual_long = (cmds.ls(actual_node, long=True) or [actual_node])[0]
    if actual_long != source_long or actual_channel != source_channel:
        raise RuntimeError(
            "实际连接仍为 {}，没有切换到 {}.{}。".format(
                actual_plug, source_long, source_channel))
    _record_branch_input(
        target_driver, move_axis, source_node, source_channel)

    for previous in previous_inputs:
        previous_node = previous.rsplit(".", 1)[0]
        if previous_node != input_node:
            _delete_orphaned_channel_node(previous_node)
    return "{}.{}".format(source_long, source_channel)


def _vector_node_axis(vector_node):
    """Finds the named or evaluated movement axis of a vector node."""
    name = _short(vector_node)
    match = re.search(r"_Secondary([XYZ])(?:_|$)", name)
    if not match:
        match = re.search(r"_QC_(?:Pos|Neg)([XYZ])(?:_|$)", name)
    if match:
        return match.group(1)
    value = cmds.getAttr(vector_node + ".input1")[0]
    index = max(range(3), key=lambda item: abs(value[item]))
    return "XYZ"[index] if abs(value[index]) > 1e-8 else None


def _surface_label_from_driver(driver, multiplier_node=None, attribute=None):
    """Returns a fixed primary label such as NegZ for an automatic driver."""
    if attribute:
        match = re.match(
            r"^qc((?:Pos|Neg)[XYZ])Move(?:[XYZ])?Multiplier$",
            attribute)
        if match:
            return match.group(1)
    if multiplier_node:
        _graph, plugs = _surface_driver_move_graph(driver)
        for plug in plugs:
            node, attr = plug.rsplit(".", 1)
            if node != multiplier_node and _short(node) != _short(multiplier_node):
                continue
            match = re.match(
                r"^qc((?:Pos|Neg)[XYZ])MoveMultiplier$", attr)
            if match:
                return match.group(1)
    name = _short(driver)
    for pattern in (
            r"_QC_((?:Pos|Neg)[XYZ])(?:_|$)",
            r"_QC_DRV_JNT_((?:Pos|Neg)[XYZ])(?:_|$)"):
        match = re.search(pattern, name)
        if match:
            return match.group(1)
    return None


def _automatic_corrective_names(source_node, label, move_axes):
    primary_axis = label[-1]
    extras = [axis for axis in "XYZ"
              if axis in move_axes and axis != primary_axis]
    suffix = "_".join([label] + extras)
    base = _short(source_node)
    return ("{}_QC_DRV_JNT_{}".format(base, suffix),
            "{}_QC_SKIN_JNT_{}".format(base, suffix))


def _matches_numbered_corrective_name(current_name, canonical_name):
    """Treats Maya's numeric collision suffix as a stable bone identity."""
    return bool(re.fullmatch(
        re.escape(canonical_name) + r"(?:\d+)?", current_name))


def repair_corrective_bone_names(driver, source_node,
                                 label=None, move_axes=None):
    """Applies the stable DRV/SKIN-before-axis naming convention."""
    if not (cmds.objExists(driver) and cmds.attributeQuery(
            "isQuaternionSurfaceDriver", node=driver, exists=True)):
        return driver
    label = label or _surface_label_from_driver(driver)
    if not label:
        return driver
    axes = tuple(move_axes or _recorded_move_axes(driver) or (label[-1],))
    driver_name, skin_name = _automatic_corrective_names(
        source_node, label, axes)
    skin_joints = _skin_joints_for_driver(driver)
    # Maya appends 1, 2, ... when another corrective already owns the
    # canonical name.  That number identifies this particular bone; it is not
    # an axis error and must not change when only the input channel is rewired.
    if not _matches_numbered_corrective_name(_short(driver), driver_name):
        driver = cmds.rename(driver, driver_name)
        long_names = cmds.ls(driver, long=True) or [driver]
        driver = long_names[0]
    for skin in skin_joints:
        if (cmds.objExists(skin) and
                not _matches_numbered_corrective_name(
                    _short(skin), skin_name)):
            cmds.rename(skin, skin_name)
    return driver


def _upstream_rotation_axis(start_node):
    """Finds the source rotate channel feeding an existing movement graph."""
    plug = _upstream_transform_channel(start_node)
    if not plug:
        return None
    attribute = plug.rsplit(".", 1)[-1]
    return attribute[-1] if attribute.startswith("rotate") else None


def _migrate_axis_metadata(driver, sum_node, main_vector, extra_slots):
    """Adds explicit axis records to a compatible rig made by an older build."""
    if _recorded_move_axes(driver):
        return
    primary_axis = _recorded_primary_move_axis(driver)
    name_match = (re.search(
        r"_QC_(?:Pos|Neg)([XYZ])(?:_|$)", _short(driver)) or
        re.search(
            r"_QC_DRV_JNT_(?:Pos|Neg)([XYZ])(?:_|$)",
            _short(driver)))
    if not primary_axis and name_match:
        primary_axis = name_match.group(1)
    if not primary_axis and main_vector:
        primary_axis = _vector_node_axis(main_vector)
    if not primary_axis:
        return

    move_axes = [primary_axis]
    for slot in extra_slots:
        vectors = cmds.listConnections(
            "{}.input3D[{}]".format(sum_node, slot),
            source=True, destination=False) or []
        if vectors:
            axis = _vector_node_axis(vectors[0])
            if axis and axis not in move_axes:
                move_axes.append(axis)
    rotation_axis = (_recorded_rotation_axis(driver) or
                     (_upstream_rotation_axis(main_vector)
                      if main_vector else None))
    _record_axis_metadata(
        driver, rotation_axis, primary_axis, move_axes)


def _quaternion_weight_attr(source, current_value=0.5):
    """Ensures quaternion weight has a +0.5 reset/default and current value."""
    plug = _ensure_attr(
        source, "quatCorrectiveWeight", 0.5, -1.0, 1.0)
    # Existing source joints may retain the old negative default even after
    # their corrective hierarchy has been rebuilt.
    cmds.addAttr(plug, edit=True, defaultValue=0.5)
    cmds.setAttr(plug, float(current_value))
    return plug


def _match_joint_axes_to_source(root, source):
    """Copies source world axes to a driver joint without occupying rotate.

    The quaternion graph owns ``root.rotate``.  Storing the rest orientation in
    rotateAxis (rather than freezing into jointOrient) keeps that input free
    and, importantly, preserves the source joint's actual world axes.
    """
    source_matrix = cmds.xform(source, query=True, worldSpace=True, matrix=True)
    cmds.xform(root, worldSpace=True, matrix=source_matrix)
    local_rotation = cmds.getAttr(root + ".rotate")[0]
    cmds.setAttr(root + ".rotateAxis", *local_rotation, type="double3")
    cmds.setAttr(root + ".rotate", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(root + ".jointOrient", 0.0, 0.0, 0.0, type="double3")


def _orient_constraints_driving(node):
    """Returns orient constraints connected to a node's rotate channels."""
    constraints = []
    plugs = [node + ".rotate"] + [node + ".rotate" + axis for axis in "XYZ"]
    for plug in plugs:
        constraints.extend(cmds.listConnections(
            plug, source=True, destination=False,
            type="orientConstraint") or [])
    return list(dict.fromkeys(constraints))


def _connect_quaternion(source_node, source_attr, destination_node,
                        destination_attr):
    for component in "XYZW":
        _connect(
            "{}.{}{}".format(source_node, source_attr, component),
            "{}.{}{}".format(destination_node, destination_attr, component))


def _make_local_quaternion_network(source, root, buffer, inverse, weight_attr):
    """Drives QC_ROOT from the source joint's local rotation quaternion.

    The buffer follows only the source parent, retaining the neutral source
    axes.  The root receives a quaternion interpolation from identity to the
    source's local ``rotate`` value.  This separates global parent motion from
    the actual bend and prevents jointOrient from turning a vertical bend into
    a horizontal world-space twist.
    """
    parent = (cmds.listRelatives(
        source, parent=True, fullPath=True) or [None])[0]
    if not parent:
        raise RuntimeError(
            "{} 需要父级关节来创建局部四元数驱动。".format(source))
    try:
        cmds.loadPlugin("quatNodes", quiet=True)
    except RuntimeError as error:
        raise RuntimeError("无法加载 quatNodes 插件：{}".format(error))
    prefix = _short(source) + "_QC_Root"
    parent_follow = cmds.orientConstraint(
        parent, buffer, maintainOffset=True,
        name=prefix + "_ParentFollow")[0]
    if cmds.attributeQuery("interpType", node=parent_follow, exists=True):
        cmds.setAttr(parent_follow + ".interpType", 1)

    euler_to_quat = cmds.createNode(
        "eulerToQuat", name=prefix + "_LocalEulerToQuat")
    for axis in "XYZ":
        _connect(source + ".rotate" + axis,
                 euler_to_quat + ".inputRotate" + axis)
    _connect(source + ".rotateOrder", euler_to_quat + ".inputRotateOrder")

    quaternion_source = euler_to_quat
    quaternion_attr = "outputQuat"
    nodes = [parent_follow, euler_to_quat]
    if inverse:
        invert = cmds.createNode(
            "quatInvert", name=prefix + "_LocalQuatInverse")
        _connect_quaternion(
            euler_to_quat, "outputQuat", invert, "inputQuat")
        quaternion_source = invert
        nodes.append(invert)

    slerp = cmds.createNode("quatSlerp", name=prefix + "_LocalQuatSlerp")
    cmds.setAttr(slerp + ".input1QuatX", 0.0)
    cmds.setAttr(slerp + ".input1QuatY", 0.0)
    cmds.setAttr(slerp + ".input1QuatZ", 0.0)
    cmds.setAttr(slerp + ".input1QuatW", 1.0)
    cmds.setAttr(slerp + ".angleInterpolation", 0)
    _connect_quaternion(
        quaternion_source, quaternion_attr, slerp, "input2Quat")
    _connect(weight_attr, slerp + ".inputT")

    quat_to_euler = cmds.createNode(
        "quatToEuler", name=prefix + "_LocalQuatToEuler")
    _connect_quaternion(slerp, "outputQuat", quat_to_euler, "inputQuat")
    _connect(root + ".rotateOrder", quat_to_euler + ".inputRotateOrder")
    _connect(quat_to_euler + ".outputRotate", root + ".rotate")
    nodes.extend([slerp, quat_to_euler])

    source_attr = "qcQuaternionSource"
    if not cmds.attributeQuery(source_attr, node=root, exists=True):
        cmds.addAttr(root, longName=source_attr, dataType="string")
    cmds.setAttr(root + "." + source_attr,
                 cmds.ls(source, long=True)[0], type="string")
    inverse_attr = "qcQuaternionInverse"
    if not cmds.attributeQuery(inverse_attr, node=root, exists=True):
        cmds.addAttr(root, longName=inverse_attr, attributeType="bool")
    cmds.setAttr(root + "." + inverse_attr, bool(inverse))
    return tuple(nodes)


def _make_root_quaternion_network(source, root, inverse, weight_attr):
    """Drives QC_ROOT from local bend inside a source-aligned reference group.

    The buffer records the original source axes and follows only the source
    parent.  QC_ROOT receives the source joint's local rotation quaternion, so
    global motion and jointOrient do not contaminate the bend direction.
    """
    parent = (cmds.listRelatives(source, parent=True, fullPath=True) or [None])[0]
    if not parent:
        raise RuntimeError("{} 需要父级关节来创建 quatCorrectiveWeight 混合。".format(source))
    prefix = _short(source) + "_QC_Root"
    root_parent = (cmds.listRelatives(root, parent=True, fullPath=True) or [None])[0]
    buffer = prefix + "_QuatBlend_GRP"
    if not cmds.objExists(buffer):
        buffer = cmds.createNode("transform", name=buffer, parent=root_parent)
        cmds.xform(buffer, worldSpace=True, translation=cmds.xform(root, query=True, worldSpace=True, translation=True))
    # A partially deleted system from an older tool build may leave the old
    # orientConstraint on this group.  Never allow that legacy connection to
    # survive into a newly created root network.
    legacy_group_constraints = _orient_constraints_driving(buffer)
    if legacy_group_constraints:
        cmds.delete(legacy_group_constraints)
    remaining_rotate_inputs = []
    for plug in [buffer + ".rotate"] + [buffer + ".rotate" + axis for axis in "XYZ"]:
        remaining_rotate_inputs.extend(cmds.listConnections(
            plug, source=True, destination=False, plugs=True) or [])
    if remaining_rotate_inputs:
        raise RuntimeError(
            "{} 仍有外部旋转输入（{}），无法作为静态轴向组。".format(
                buffer, remaining_rotate_inputs[0]))
    cmds.matchTransform(buffer, source, position=False, rotation=True, scale=False)
    if (cmds.listRelatives(root, parent=True, fullPath=False) or [None])[0] != buffer:
        cmds.parent(root, buffer)
    # QC_ROOT starts as a zero-offset child of the source-aligned group.  The
    # constraint below owns its rotate channels.
    cmds.setAttr(root + ".jointOrient", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(root + ".rotateAxis", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(root + ".rotate", 0.0, 0.0, 0.0, type="double3")
    quaternion_nodes = _make_local_quaternion_network(
        source, root, buffer, inverse, weight_attr)
    return tuple([buffer] + list(quaternion_nodes))


def _make_rotation_distance(source, label, rotation_axis, positive, base_distance, multiplier_attr):
    prefix = _short(source) + "_QC_" + label
    clamp = cmds.createNode("clamp", name=prefix + "_BendClamp")
    cmds.setAttr(clamp + ".minR", 0.0)
    cmds.setAttr(clamp + ".maxR", 360.0)
    rotate_plug = source + ".rotate" + rotation_axis
    nodes = [clamp]
    if positive:
        _connect(rotate_plug, clamp + ".inputR")
    else:
        invert = cmds.createNode("multDoubleLinear", name=prefix + "_NegativeBend")
        cmds.setAttr(invert + ".input2", -1.0)
        _connect(rotate_plug, invert + ".input1")
        _connect(invert + ".output", clamp + ".inputR")
        nodes.append(invert)
    angle_to_distance = cmds.createNode("multDoubleLinear", name=prefix + "_AngleToDistance")
    cmds.setAttr(angle_to_distance + ".input2", base_distance)
    _connect(clamp + ".outputR", angle_to_distance + ".input1")
    distance = cmds.createNode("multDoubleLinear", name=prefix + "_RotationMoveDistance")
    _connect(angle_to_distance + ".output", distance + ".input1")
    _connect(multiplier_attr, distance + ".input2")
    nodes.extend([angle_to_distance, distance])
    return distance, nodes


def _make_rotation_delta_distance(source, label, rotation_axis, base_distance,
                                  multiplier_attr):
    """Maps signed rotation change from the current pose to a move distance.

    This is used by manually added corrective bones.  The pose at link creation
    becomes the zero point, so creating a link never makes the positioned bone
    jump. Positive and negative rotation changes remain available to the rig.
    """
    prefix = _short(source) + "_QC_" + label
    rotate_plug = source + ".rotate" + rotation_axis
    reference_angle = cmds.getAttr(rotate_plug)

    delta = cmds.createNode("addDoubleLinear", name=prefix + "_RotationDelta")
    cmds.setAttr(delta + ".input1", -reference_angle)
    _connect(rotate_plug, delta + ".input2")

    angle_to_distance = cmds.createNode(
        "multDoubleLinear", name=prefix + "_AngleToDistance")
    cmds.setAttr(angle_to_distance + ".input2", base_distance)
    _connect(delta + ".output", angle_to_distance + ".input1")

    distance = cmds.createNode(
        "multDoubleLinear", name=prefix + "_RotationMoveDistance")
    _connect(angle_to_distance + ".output", distance + ".input1")
    _connect(multiplier_attr, distance + ".input2")
    return distance, (delta, angle_to_distance, distance)


def _axis_vector(axis, sign=1.0):
    """Returns a vector in the corrective root's local parent space."""
    vectors = {"X": (sign, 0.0, 0.0),
               "Y": (0.0, sign, 0.0),
               "Z": (0.0, 0.0, sign)}
    if axis not in vectors:
        raise RuntimeError("{} 不是有效的位移轴。".format(axis))
    return vectors[axis]


def _configure_move_vector(vector_node, axis, sign=1.0):
    """Configures a multiplyDivide vector for one local translation axis."""
    cmds.setAttr(vector_node + ".input1", *_axis_vector(axis, sign), type="double3")


def _connect_distance_to_vector(distance_plug, vector_node):
    """Feeds one scalar distance into all multiplyDivide input2 components."""
    for channel in "XYZ":
        _connect(distance_plug + ".output", vector_node + ".input2" + channel)


def _configure_surface_driver_axis(driver, label, vector_node):
    """Makes a Pos/Neg surface joint face and move along its selected axis."""
    axis = label[-1]
    sign = 1.0 if label.startswith("Pos") else -1.0
    cmds.setAttr(driver + ".jointOrient", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(driver + ".rotate", 0.0, 0.0, 0.0, type="double3")
    cmds.setAttr(driver + ".rotateAxis", 0.0, 0.0, 0.0, type="double3")
    _configure_move_vector(vector_node, axis, sign)


def _make_surface_driver(source, root, label, surface_position, distance_plug,
                         second_axis=None, second_distance_plug=None,
                         additional_movements=None):
    """Creates one Pos/Neg driver, optionally with extra local move axes.

    Both vectors are added in the driver's parent (corrective-root) space.  A
    constrained skin joint inherits the resulting translation automatically,
    so it must not receive a second, separate connection.
    """
    prefix = _short(source) + "_QC_" + label
    extra_movements = list(additional_movements or [])
    # ``second_*`` keeps Python callers from the preceding tool version valid.
    if second_axis in ("X", "Y", "Z") and second_distance_plug:
        extra_movements.insert(0, (second_axis, second_distance_plug))
    move_axes = [label[-1]] + [axis for axis, _plug in extra_movements]
    driver_name, _skin_name = _automatic_corrective_names(
        source, label, move_axes)
    driver = cmds.createNode("joint", name=driver_name, parent=root)
    # A fresh child joint already has zero local rotation and therefore inherits
    # the corrective root/source axes exactly.  Do not matchTransform here:
    # Maya may bake an inverse joint rotation into the child.
    cmds.xform(driver, worldSpace=True, translation=surface_position)
    cmds.setAttr(
        driver + ".radius",
        cmds.getAttr(source + ".radius") + CORRECTIVE_RADIUS_OFFSET)
    cmds.setAttr(driver + ".segmentScaleCompensate", 0)
    cmds.addAttr(driver, longName="isQuaternionSurfaceDriver", attributeType="bool", defaultValue=True)
    cmds.setAttr(driver + ".isQuaternionSurfaceDriver", lock=True)

    rest = cmds.getAttr(driver + ".translate")[0]
    vector = cmds.createNode("multiplyDivide", name=prefix + "_RotationMoveVector")
    _configure_surface_driver_axis(driver, label, vector)
    _connect_distance_to_vector(distance_plug, vector)
    add = cmds.createNode("plusMinusAverage", name=prefix + "_TranslateSum")
    cmds.setAttr(add + ".operation", 1)
    cmds.setAttr(add + ".input3D[0]", *rest, type="double3")
    _connect(vector + ".output", add + ".input3D[1]")
    nodes = [vector, add]
    for index, (axis, extra_distance_plug) in enumerate(extra_movements, start=2):
        extra_vector = cmds.createNode(
            "multiplyDivide", name=prefix + "_Secondary" + axis + "_RotationMoveVector")
        # Extra-axis multipliers support positive and negative values, so
        # +parent-axis is the neutral/default direction for either surface side.
        _configure_move_vector(extra_vector, axis)
        _connect_distance_to_vector(extra_distance_plug, extra_vector)
        _connect(extra_vector + ".output", add + ".input3D[{}]".format(index))
        nodes.append(extra_vector)
    _connect(add + ".output3D", driver + ".translate")
    return driver, tuple(nodes)


def _make_skin_joint(driver, skin_parent, name):
    skin = cmds.createNode("joint", name=name, parent=skin_parent)
    cmds.matchTransform(skin, driver, position=True, rotation=True, scale=False)
    cmds.setAttr(skin + ".radius", cmds.getAttr(driver + ".radius"))
    cmds.setAttr(skin + ".segmentScaleCompensate", 0)
    cmds.addAttr(skin, longName="isQuaternionCorrectiveSkinJoint", attributeType="bool", defaultValue=True)
    cmds.setAttr(skin + ".isQuaternionCorrectiveSkinJoint", lock=True)
    cmds.parentConstraint(driver, skin, maintainOffset=False, name=name + "_parentConstraint")
    cmds.scaleConstraint(driver, skin, maintainOffset=False, name=name + "_scaleConstraint")
    return skin


def _second_axis_from_settings(settings):
    """Reads the optional extra translation axis (keeps old settings compatible)."""
    return settings.get("second_axis", settings.get("manual_second_axis", "无"))


def _second_distance_from_settings(settings):
    return settings.get("second_base_distance", settings.get("manual_second_distance", 0.02))


def _additional_move_axes(settings, primary_axis):
    """Gets optional extra axes while accepting settings from older UI builds.

    The Pos/Neg surface axis is always the primary movement axis.  The UI may
    add either or both of the remaining axes, yielding a full X/Y/Z movement
    graph without duplicating the primary branch.
    """
    requested = settings.get("extra_axes")
    if requested is None:
        legacy_axis = _second_axis_from_settings(settings)
        requested = [legacy_axis] if legacy_axis in ("X", "Y", "Z") else []
    return [axis for axis in "XYZ" if axis in requested and axis != primary_axis]


def _creation_surface_axes(settings):
    """Returns the ordered joint axes selected for automatic creation."""
    requested = settings.get("surface_axes")
    if requested is None:
        legacy_axis = settings.get("surface_axis")
        requested = [legacy_axis] if legacy_axis in ("X", "Y", "Z") else []
    axes = [axis for axis in "XYZ" if axis in requested]
    if not axes:
        raise RuntimeError("请至少勾选一个关节创建方向。")
    return axes


def _validate_move_settings(settings):
    """Reject settings that would create a visually inert move-multiplier rig."""
    if abs(settings["base_distance"]) < 1e-8:
        raise RuntimeError("主轴每度基础位移不能为 0；否则 qcPos/Neg*MoveMultiplier 不会产生位移。")
    if settings.get("probe_distance", 1.0) <= 0.0:
        raise RuntimeError("表面探测距离必须大于 0。")
    for surface_axis in _creation_surface_axes(settings):
        if (_additional_move_axes(settings, surface_axis) and
                abs(_second_distance_from_settings(settings)) < 1e-8):
            raise RuntimeError("已勾选附加位移轴时，附加轴每度基础位移不能为 0。")


def _positive_rotation_for(source, settings):
    """Resolves the bend sign without making a Knee use an Elbow default.

    The source labels (Pos/Neg) describe the two surface sides, not the sign
    of the bend.  Both sides must therefore share one bend signal.  In auto
    mode the requested ADV defaults are used: elbows bend on +, knees on -.
    Direct Python callers using the older ``positive_rotation`` setting keep
    their existing behaviour.
    """
    direction = settings.get("bend_direction")
    if direction == "+":
        return True
    if direction == "-":
        return False
    if direction == "自动":
        return "knee" not in _short(source).lower()
    return settings.get("positive_rotation", True)


def _secondary_multiplier_name(label, axis):
    """Artist-facing multiplier name, e.g. qcNegYMoveXMultiplier."""
    return "qc{}Move{}Multiplier".format(label, axis)


def _translate_sum_movement(sum_node):
    """Returns the total dynamic offset from every TranslateSum movement slot."""
    total = [0.0, 0.0, 0.0]
    indices = cmds.getAttr(sum_node + ".input3D", multiIndices=True) or []
    for index in indices:
        if index < 1:
            continue
        value = cmds.getAttr("{}.input3D[{}]".format(sum_node, index))[0]
        for channel in range(3):
            total[channel] += value[channel]
    return tuple(total)


def _manual_multiplier_name(driven, axis=None, primary_axis=None):
    safe = "".join(character if character.isalnum() else "_" for character in _short(driven))
    if axis in ("X", "Y", "Z") and axis != primary_axis:
        return "qcManual_{}Move{}Multiplier".format(safe, axis)
    return "qcManual_{}MoveMultiplier".format(safe)


def _remove_previous_manual_constraints(driven):
    """Removes only constraints made by this tool's retired manual-link mode."""
    constraints = []
    for constraint_type in ("parentConstraint", "scaleConstraint"):
        constraints.extend(cmds.listConnections(driven, source=True, destination=False, type=constraint_type) or [])
    for constraint in set(constraints):
        if cmds.attributeQuery("isManualCorrectiveDriveLink", node=constraint, exists=True):
            cmds.delete(constraint)


def create_manual_drive_link(driven, driver, settings):
    """Creates the same rotation-to-translation logic for a manually made bone.

    ``driven`` is intentionally the first selection and ``driver`` the second.
    The driven bone retains its hand-authored orientation; only translate is
    driven by the selected driver joint's bend rotation.
    """
    _validate_move_settings(settings)
    if not cmds.objExists(driven) or not cmds.objExists(driver):
        raise RuntimeError("被驱动骨骼或驱动骨骼不存在。")
    if driven == driver:
        raise RuntimeError("被驱动骨骼与驱动骨骼不能是同一个节点。")
    valid_types = ("joint", "transform")
    if cmds.nodeType(driven) not in valid_types or cmds.nodeType(driver) not in valid_types:
        raise RuntimeError("驱动链接仅支持 joint 或 transform 节点。")
    _remove_previous_manual_constraints(driven)
    prefix = "{}_Manual_{}".format(_short(driver), _short(driven))
    # Upgrade a previous version of this tool's multi-axis manual graph.
    old_nodes = cmds.ls(prefix + "_*", long=False) or []
    old_nodes.extend(cmds.ls(_short(driver) + "_QC_Manual*_" + _short(driven) + "_*", long=False) or [])
    if old_nodes:
        cmds.delete(list(set(old_nodes)))
    incoming = cmds.listConnections(driven + ".translate", source=True, destination=False, plugs=True) or []
    if incoming:
        raise RuntimeError("{} 已有位移输入（{}）。请先断开后再创建手动旋转位移驱动。".format(_short(driven), incoming[0]))

    axis = settings["surface_axis"]
    extra_axes = _additional_move_axes(settings, axis)
    signed_rotation = settings.get("signed_rotation", False)
    saved_multiplier_plugs = settings.get("manual_multiplier_plugs", {})
    rest = cmds.getAttr(driven + ".translate")[0]

    if signed_rotation:
        translate_sum = cmds.createNode(
            "plusMinusAverage", name=prefix + "_TranslateSum")
        cmds.setAttr(translate_sum + ".operation", 1)
        cmds.setAttr(translate_sum + ".input3D[0]", *rest, type="double3")
        nodes = [translate_sum]
        movements = [(axis, settings["base_distance"])]
        movements.extend(
            (extra_axis, _second_distance_from_settings(settings))
            for extra_axis in extra_axes)

        for slot, (move_axis, base_distance) in enumerate(movements, 1):
            multiplier_attr = saved_multiplier_plugs.get(move_axis)
            if not multiplier_attr or not cmds.objExists(multiplier_attr):
                multiplier_attr = _ensure_attr(
                    driver, _manual_multiplier_name(
                        driven, move_axis, axis),
                    0.0, -10.0, 10.0)
            label = "Manual{}_{}".format(move_axis, _short(driven))
            distance, distance_nodes = _make_rotation_delta_distance(
                driver, label, settings["rotation_axis"], base_distance,
                multiplier_attr)
            vector = cmds.createNode(
                "multiplyDivide",
                name=prefix + "_{}OwnAxisMoveVector".format(move_axis))
            direction = _node_axis_in_parent_space(driven, move_axis)
            cmds.setAttr(vector + ".input1", *direction, type="double3")
            _connect_distance_to_vector(distance, vector)
            _connect(
                vector + ".output",
                "{}.input3D[{}]".format(translate_sum, slot))
            nodes.extend(list(distance_nodes) + [vector])

        _connect(translate_sum + ".output3D", driven + ".translate")
        if not cmds.attributeQuery(
                "isQuaternionManualMoveTarget", node=driven, exists=True):
            cmds.addAttr(
                driven, longName="isQuaternionManualMoveTarget",
                attributeType="bool", defaultValue=True)
            cmds.setAttr(driven + ".isQuaternionManualMoveTarget", lock=True)
        _record_axis_metadata(
            driven, settings["rotation_axis"], axis, [axis] + extra_axes)
        for move_axis in [axis] + extra_axes:
            _record_branch_input(
                driven, move_axis, driver,
                "rotate" + settings["rotation_axis"])
        return tuple(nodes)

    positive_rotation = _positive_rotation_for(driver, settings)
    driver_position = cmds.xform(
        driver, query=True, worldSpace=True, translation=True)
    driven_position = cmds.xform(
        driven, query=True, worldSpace=True, translation=True)

    def make_axis_drive(move_axis, base_distance):
        multiplier_attr = saved_multiplier_plugs.get(move_axis)
        if not multiplier_attr or not cmds.objExists(multiplier_attr):
            multiplier_attr = _ensure_attr(
                driver, _manual_multiplier_name(
                    driven, move_axis, axis),
                0.0, -10.0, 10.0)
        world_axis = _source_axis_world(driver, move_axis)
        side = 1.0 if sum(
            (driven_position[index] - driver_position[index]) * world_axis[index]
            for index in range(3)) >= 0.0 else -1.0
        side_name = "Pos" if side > 0.0 else "Neg"
        label = "Manual{}{}_{}".format(side_name, move_axis, _short(driven))
        distance, distance_nodes = _make_rotation_distance(
            driver, label, settings["rotation_axis"], positive_rotation,
            base_distance, multiplier_attr)
        signed_distance = cmds.createNode(
            "multDoubleLinear",
            name=prefix + "_{}SignedRotationMoveDistance".format(move_axis))
        cmds.setAttr(signed_distance + ".input2", side)
        _connect(distance + ".output", signed_distance + ".input1")
        distance_output = signed_distance + ".output"
        result_nodes = list(distance_nodes) + [signed_distance]
        channel_index = "XYZ".index(move_axis)
        translate_sum = cmds.createNode("addDoubleLinear", name=prefix + "_Translate{}Sum".format(move_axis))
        cmds.setAttr(translate_sum + ".input1", rest[channel_index])
        _connect(distance_output, translate_sum + ".input2")
        _connect(translate_sum + ".output", driven + ".translate" + move_axis)
        result_nodes.append(translate_sum)
        return tuple(result_nodes)

    nodes = list(make_axis_drive(axis, settings["base_distance"]))
    for extra_axis in extra_axes:
        nodes.extend(make_axis_drive(extra_axis, _second_distance_from_settings(settings)))
    if not cmds.attributeQuery("isQuaternionManualMoveTarget", node=driven, exists=True):
        cmds.addAttr(driven, longName="isQuaternionManualMoveTarget", attributeType="bool", defaultValue=True)
        cmds.setAttr(driven + ".isQuaternionManualMoveTarget", lock=True)
    _record_axis_metadata(
        driven, settings["rotation_axis"], axis, [axis] + extra_axes)
    for move_axis in [axis] + extra_axes:
        _record_branch_input(
            driven, move_axis, driver,
            "rotate" + settings["rotation_axis"])
    return tuple(nodes)


def _manual_drive_version(driver):
    """Returns 0 for an unversioned legacy manual graph."""
    attribute = "qcManualDriveVersion"
    if not cmds.attributeQuery(attribute, node=driver, exists=True):
        return 0
    try:
        return int(cmds.getAttr(driver + "." + attribute))
    except (TypeError, ValueError, RuntimeError):
        return 0


def _record_manual_drive_version(driver):
    _set_locked_axis_attr(
        driver, "qcManualDriveVersion", MANUAL_DRIVE_VERSION, "long")


def _normalized_scene_plug(plug):
    """Normalizes a transform plug so renamed/long DAG paths compare safely."""
    if not plug or "." not in plug:
        return plug or ""
    node, attribute = plug.rsplit(".", 1)
    long_names = cmds.ls(node, long=True) or [node]
    return "{}.{}".format(long_names[0], attribute)


def _capture_manual_graph_state(driver):
    """Captures artist values and branch settings before deleting old nodes."""
    graph_nodes, multiplier_plugs = _surface_driver_move_graph(driver)
    primary_axis = _recorded_primary_move_axis(driver)
    move_axes = list(_recorded_move_axes(driver))
    rotation_axis = _recorded_rotation_axis(driver)
    records = {}

    for plug in multiplier_plugs:
        multiplier_node, attribute = plug.rsplit(".", 1)
        target, move_axis = _connected_multiplier_info(
            multiplier_node, attribute)
        if target and not _same_node(target, driver):
            continue
        if not move_axis:
            move_axis = primary_axis
        if move_axis not in ("X", "Y", "Z"):
            continue
        _distance_node, angle_node = _movement_branch_nodes(
            multiplier_node, attribute, driver, move_axis)
        base_distance = None
        if angle_node and cmds.objExists(angle_node + ".input2"):
            base_distance = float(cmds.getAttr(angle_node + ".input2"))
        records[move_axis] = {
            "plug": _normalized_scene_plug(plug),
            "value": float(cmds.getAttr(plug)),
            "base_distance": base_distance,
            "input_plug": _movement_branch_input(
                multiplier_node, attribute, driver, move_axis),
        }

    if not primary_axis and records:
        primary_axis = next(
            (axis for axis in "XYZ" if axis in records), None)
    if not move_axes:
        move_axes = [axis for axis in "XYZ" if axis in records]
    if primary_axis and primary_axis not in move_axes:
        move_axes.insert(0, primary_axis)
    return {
        "graph_nodes": graph_nodes,
        "multiplier_plugs": multiplier_plugs,
        "records": records,
        "primary_axis": primary_axis,
        "move_axes": move_axes,
        "rotation_axis": rotation_axis,
        "local_translate": tuple(cmds.getAttr(driver + ".translate")[0]),
    }


def _manual_migration_settings(settings, state):
    """Keeps recorded axes, base distances and multiplier plugs during upgrade."""
    migrated = dict(settings)
    primary_axis = state.get("primary_axis") or settings["surface_axis"]
    move_axes = list(state.get("move_axes") or [primary_axis])
    if primary_axis not in move_axes:
        move_axes.insert(0, primary_axis)
    migrated["surface_axis"] = primary_axis
    migrated["extra_axes"] = [
        axis for axis in "XYZ"
        if axis in move_axes and axis != primary_axis]
    migrated["rotation_axis"] = (
        state.get("rotation_axis") or settings["rotation_axis"])
    migrated["signed_rotation"] = True
    migrated["manual_multiplier_plugs"] = {
        axis: record["plug"]
        for axis, record in state.get("records", {}).items()
        if record.get("plug") and cmds.objExists(record["plug"])
    }

    primary_record = state.get("records", {}).get(primary_axis, {})
    primary_distance = primary_record.get("base_distance")
    if primary_distance is not None and abs(primary_distance) > 1e-8:
        migrated["base_distance"] = primary_distance
    extra_distances = [
        state["records"][axis].get("base_distance")
        for axis in migrated["extra_axes"]
        if axis in state.get("records", {})]
    extra_distance = next(
        (value for value in extra_distances
         if value is not None and abs(value) > 1e-8), None)
    if extra_distance is not None:
        migrated["second_base_distance"] = extra_distance
        migrated["manual_second_distance"] = extra_distance
    return migrated


def _restore_migrated_manual_branches(driver, source, settings, state):
    """Restores exact values/base distances/custom inputs after graph rebuild."""
    primary_axis = settings["surface_axis"]
    default_input = _normalized_scene_plug(
        source + ".rotate" + settings["rotation_axis"])
    multiplier_overrides = settings.get("manual_multiplier_plugs", {})

    for move_axis in [primary_axis] + _additional_move_axes(
            settings, primary_axis):
        multiplier_plug = multiplier_overrides.get(move_axis)
        if not multiplier_plug or not cmds.objExists(multiplier_plug):
            multiplier_plug = "{}.{}".format(
                source,
                _manual_multiplier_name(
                    driver, move_axis, primary_axis))
        if not cmds.objExists(multiplier_plug):
            continue
        multiplier_plug = _normalized_scene_plug(multiplier_plug)
        record = state.get("records", {}).get(move_axis)
        if not record:
            continue
        if cmds.getAttr(multiplier_plug, settable=True):
            cmds.setAttr(multiplier_plug, record["value"])
        multiplier_node, attribute = multiplier_plug.rsplit(".", 1)
        _distance_node, angle_node = _movement_branch_nodes(
            multiplier_node, attribute, driver, move_axis)
        base_distance = record.get("base_distance")
        if (angle_node and base_distance is not None and
                cmds.objExists(angle_node + ".input2")):
            cmds.setAttr(angle_node + ".input2", base_distance)

        old_input = _normalized_scene_plug(record.get("input_plug"))
        if (old_input and old_input != default_input and
                cmds.objExists(old_input)):
            input_node, input_channel = old_input.rsplit(".", 1)
            rewire_movement_branch(
                multiplier_node, attribute, driver, move_axis,
                input_node, input_channel)

    # Custom inputs may currently be away from their neutral value.  Rebase the
    # rest translation so upgrading never moves the visible corrective joint.
    sum_sources = cmds.listConnections(
        driver + ".translate", source=True,
        destination=False, plugs=True) or []
    if sum_sources:
        sum_node = sum_sources[0].rsplit(".", 1)[0]
        if cmds.nodeType(sum_node) == "plusMinusAverage":
            movement = _translate_sum_movement(sum_node)
            desired = state["local_translate"]
            base = tuple(
                desired[index] - movement[index] for index in range(3))
            cmds.setAttr(
                sum_node + ".input3D[0]", *base, type="double3")


def _rebuild_legacy_manual_graph(driver, source, settings, state,
                                 target_parent):
    """Replaces only private manual calculation nodes with the current graph."""
    old_nodes = [
        node for node in state.get("graph_nodes", [])
        if cmds.objExists(node)]
    if old_nodes:
        cmds.delete(old_nodes)
    parents = cmds.listRelatives(driver, parent=True, fullPath=True) or []
    if not parents or not _same_node(parents[0], target_parent):
        driver = cmds.parent(driver, target_parent, relative=True)[0]
        driver = (cmds.ls(driver, long=True) or [driver])[0]
    cmds.setAttr(
        driver + ".translate", *state["local_translate"], type="double3")
    migrated_settings = _manual_migration_settings(settings, state)
    nodes = create_manual_drive_link(driver, source, migrated_settings)
    _restore_migrated_manual_branches(
        driver, source, migrated_settings, state)
    _record_manual_drive_version(driver)
    return driver, tuple(nodes)


def _set_boolean_marker(node, attribute):
    """Adds one locked boolean rig marker, or repairs a copied marker."""
    plug = "{}.{}".format(node, attribute)
    if not cmds.attributeQuery(attribute, node=node, exists=True):
        cmds.addAttr(
            node, longName=attribute, attributeType="bool",
            defaultValue=True)
    else:
        cmds.setAttr(plug, lock=False)
        cmds.setAttr(plug, True)
    cmds.setAttr(plug, lock=True)


def _remove_copied_marker(node, attribute):
    """Removes a marker copied from a template joint before changing its role."""
    if not cmds.attributeQuery(attribute, node=node, exists=True):
        return
    plug = "{}.{}".format(node, attribute)
    cmds.setAttr(plug, lock=False)
    cmds.deleteAttr(plug)


def _manual_corrective_names(template):
    """Builds a matching DRV/SKIN name pair from the artist's template name."""
    name = _short(template)
    if "_QC_SKIN_JNT_" in name:
        driver_name = name.replace("_QC_SKIN_JNT_", "_QC_DRV_JNT_", 1)
    elif name.endswith("_QC_SKIN_JNT"):
        driver_name = name[:-len("_QC_SKIN_JNT")] + "_QC_DRV_JNT"
    elif "_QC_DRV_JNT_" in name or name.endswith("_QC_DRV_JNT"):
        driver_name = name
    else:
        driver_name = name + "_QC_DRV_JNT"
    skin_name = driver_name.replace("_QC_DRV_JNT", "_QC_SKIN_JNT", 1)
    return driver_name, skin_name


def _inside_corrective_driver_hierarchy(node):
    """Checks whether a node belongs to a generated driver hierarchy."""
    current = node
    while current and cmds.objExists(current):
        if (cmds.attributeQuery(
                "isQuaternionCorrectiveRoot", node=current, exists=True) or
                cmds.attributeQuery(
                    "isQuaternionManualDriverRoot",
                    node=current, exists=True) or
                cmds.attributeQuery(
                    "isQuaternionSurfaceDriver", node=current, exists=True)):
            return True
        parents = cmds.listRelatives(
            current, parent=True, fullPath=True) or []
        current = parents[0] if parents else None
    return False


def _record_driver_skin_pair(driver, skin):
    """Records the generated skin joint without depending on names."""
    attribute = "qcSkinJoint"
    if not cmds.attributeQuery(attribute, node=driver, exists=True):
        cmds.addAttr(driver, longName=attribute, attributeType="message")
    _connect(skin + ".message", driver + "." + attribute)


def create_manual_corrective_pair(template, source, settings):
    """Converts a positioned template into a driver and creates its skin joint.

    The template's world transform is preserved while it is moved below a
    stable group.  By default that group follows the rotation source; when
    requested it follows the template's original parent instead, while the
    movement graph still reads the rotation source.  Its former skin-hierarchy
    parent is reused for the constrained skin joint.  Legacy graphs are
    upgraded in place.
    """
    if not cmds.objExists(template) or cmds.nodeType(template) != "joint":
        raise RuntimeError("第一选择必须是需要转换的关节骨骼。")
    if not cmds.objExists(source) or cmds.nodeType(source) != "joint":
        raise RuntimeError("第二选择必须是负责旋转的源关节。")
    if _same_node(template, source):
        raise RuntimeError("摆放骨骼与旋转源关节不能是同一个节点。")
    if cmds.attributeQuery(
            "isQuaternionCorrectiveRoot", node=template, exists=True):
        raise RuntimeError("不能把驱动根骨骼作为新增修型骨骼。")

    descendants = cmds.listRelatives(
        template, allDescendents=True, type="joint", fullPath=True) or []
    if any(_same_node(node, source) for node in descendants):
        raise RuntimeError("旋转源关节不能位于摆放骨骼的子层级中。")
    if descendants:
        raise RuntimeError(
            "{} 下仍有子关节（{}）。请先只保留需要转换的这一根骨骼。".format(
                _short(template), _short(descendants[0])))

    root = _corrective_root_for_source(source)
    is_manual_target = cmds.attributeQuery(
        "isQuaternionManualMoveTarget", node=template, exists=True)
    existing_skins = _skin_joints_for_driver(template)
    if existing_skins and not is_manual_target:
        raise RuntimeError(
            "{} 已经有配套蒙皮骨骼：{}。".format(
                _short(template), _short(existing_skins[0])))
    if len(existing_skins) > 1:
        raise RuntimeError(
            "{} 连接了多个蒙皮骨骼，无法安全升级：{}。".format(
                _short(template),
                "、".join(_short(node) for node in existing_skins)))

    incoming_translate = cmds.listConnections(
        template + ".translate", source=True,
        destination=False, plugs=True) or []
    existing_manual_graph = bool(
        incoming_translate and is_manual_target)
    migration_required = bool(
        existing_manual_graph and
        _manual_drive_version(template) < MANUAL_DRIVE_VERSION)
    migration_state = (_capture_manual_graph_state(template)
                       if migration_required else None)
    if incoming_translate and not existing_manual_graph:
        raise RuntimeError(
            "{} 已有位移输入（{}），不能作为新的摆放骨骼。".format(
                _short(template), incoming_translate[0]))

    original_parents = cmds.listRelatives(
        template, parent=True, fullPath=True) or []
    original_parent = original_parents[0] if original_parents else None
    skin_parent = original_parent
    if (not skin_parent or
            _inside_corrective_driver_hierarchy(skin_parent)):
        skin_parent = source

    current_parent_is_manual = bool(
        original_parent and cmds.attributeQuery(
            "isQuaternionManualDriverRoot",
            node=original_parent, exists=True))
    if existing_manual_graph and current_parent_is_manual:
        # Rebuilding an existing driver must preserve its recorded spatial
        # follow group, regardless of the checkbox's current UI state.
        manual_parent = original_parent
    else:
        # The checkbox chooses the space only for a newly converted joint.
        # A legacy driver that is being upgraded keeps the historical
        # source-follow behaviour; changing an existing driver's space here
        # would cause an unexpected world-space jump during a rebuild.
        keep_original_space = bool(
            settings.get("keep_original_parent_space", False) and
            not existing_manual_graph)
        if keep_original_space:
            if not original_parent:
                raise RuntimeError(
                    "已勾选“保持原父级空间”，但 {} 没有父级。".format(
                        _short(template)))
            if _inside_corrective_driver_hierarchy(original_parent):
                raise RuntimeError(
                    "{} 的原父级已位于修型驱动层级中，不能作为独立空间。".format(
                        _short(template)))
            follow_node = original_parent
        else:
            follow_node = source
        manual_parent = _manual_driver_parent_for_source(
            source, root, follow_node)
        current_parent_is_manual = bool(
            original_parent and _same_node(original_parent, manual_parent))
    if (existing_manual_graph and not current_parent_is_manual and
            not migration_required):
        raise RuntimeError(
            "{} 的新增驱动版本已是当前版本，但不在稳定父级下。".format(
                _short(template)))

    world_matrix = cmds.xform(
        template, query=True, worldSpace=True, matrix=True)
    driver_name, requested_skin_name = _manual_corrective_names(template)
    driver = cmds.rename(template, driver_name)
    driver = (cmds.ls(driver, long=True) or [driver])[0]
    if not current_parent_is_manual and not migration_required:
        driver = cmds.parent(driver, manual_parent, relative=True)[0]
        driver = (cmds.ls(driver, long=True) or [driver])[0]
        cmds.xform(driver, worldSpace=True, matrix=world_matrix)

    # A duplicated skin/driver joint may carry disconnected marker attributes.
    # Reassign them only after all validation has succeeded.
    _remove_copied_marker(driver, "isQuaternionCorrectiveSkinJoint")
    if not existing_manual_graph:
        _remove_copied_marker(driver, "isQuaternionSurfaceDriver")
        _remove_copied_marker(driver, "isQuaternionManualMoveTarget")
        graph_nodes = create_manual_drive_link(driver, source, settings)
        _record_manual_drive_version(driver)
    elif migration_required:
        driver, graph_nodes = _rebuild_legacy_manual_graph(
            driver, source, settings, migration_state, manual_parent)
    else:
        graph_nodes = ()
    _set_boolean_marker(driver, "isQuaternionSurfaceDriver")
    _set_boolean_marker(driver, "isQuaternionManualMoveTarget")

    actual_driver_name = _short(driver)
    if "_QC_DRV_JNT" in actual_driver_name:
        skin_name = actual_driver_name.replace(
            "_QC_DRV_JNT", "_QC_SKIN_JNT", 1)
    else:
        skin_name = requested_skin_name
    skin = (existing_skins[0] if existing_skins else
            _make_skin_joint(driver, skin_parent, skin_name))
    _record_driver_skin_pair(driver, skin)
    return driver, skin, tuple(graph_nodes)


def _surface_driver_from_selection(node):
    """Resolves a selected driver or constrained skin joint to its surface driver."""
    if cmds.objExists(node) and cmds.attributeQuery("isQuaternionSurfaceDriver", node=node, exists=True):
        return node
    constraints = cmds.listConnections(node, source=True, destination=False, type="parentConstraint") or []
    for constraint in constraints:
        for target in cmds.parentConstraint(constraint, query=True, targetList=True) or []:
            if cmds.attributeQuery("isQuaternionSurfaceDriver", node=target, exists=True):
                return target
    return None


def _surface_drivers_under_root(root):
    """Returns automatic and stable-parent manual drivers in one QC system."""
    descendants = []
    for container in [root] + _manual_driver_parents_for_root(root):
        descendants.extend(cmds.listRelatives(
            container, allDescendents=True,
            type="joint", fullPath=True) or [])
    result = []
    for node in descendants:
        if (cmds.attributeQuery(
                "isQuaternionSurfaceDriver", node=node, exists=True) and
                node not in result):
            result.append(node)
    return result


def _corrective_root_from_selection(node):
    """Resolves a selected driver root or its constrained skin-root joint."""
    if (cmds.objExists(node) and cmds.attributeQuery(
            "isQuaternionCorrectiveRoot", node=node, exists=True)):
        return node
    if (cmds.objExists(node) and cmds.attributeQuery(
            "isQuaternionManualDriverRoot", node=node, exists=True) and
            cmds.attributeQuery(
                "qcCorrectiveRoot", node=node, exists=True)):
        roots = cmds.listConnections(
            node + ".qcCorrectiveRoot",
            source=True, destination=False) or []
        if roots:
            return roots[0]
    constraints = cmds.listConnections(
        node, source=True, destination=False,
        type="parentConstraint") or []
    for constraint in constraints:
        for target in cmds.parentConstraint(
                constraint, query=True, targetList=True) or []:
            if cmds.attributeQuery(
                    "isQuaternionCorrectiveRoot",
                    node=target, exists=True):
                return target
    return None


def _skin_joints_for_driver(driver):
    """Returns skin corrective joints constrained by one surface driver."""
    result = []
    if cmds.attributeQuery("qcSkinJoint", node=driver, exists=True):
        for joint in cmds.listConnections(
                driver + ".qcSkinJoint", source=True,
                destination=False, type="joint") or []:
            if (cmds.attributeQuery(
                    "isQuaternionCorrectiveSkinJoint",
                    node=joint, exists=True) and joint not in result):
                result.append(joint)
    constraints = cmds.listConnections(
        driver, source=False, destination=True,
        type="parentConstraint") or []
    for constraint in constraints:
        joints = cmds.listConnections(
            constraint, source=False, destination=True,
            type="joint") or []
        for joint in joints:
            if (cmds.attributeQuery(
                    "isQuaternionCorrectiveSkinJoint",
                    node=joint, exists=True) and joint not in result):
                result.append(joint)
    return result


def _surface_driver_move_graph(driver):
    """Collects the private translate graph and multiplier attrs of a driver."""
    allowed_types = {
        "plusMinusAverage", "multiplyDivide", "multDoubleLinear",
        "addDoubleLinear", "clamp", "condition", "unitConversion"}
    queue = cmds.listConnections(
        driver + ".translate", source=True, destination=False) or []
    graph_nodes = []
    multiplier_plugs = []
    visited = set()
    while queue:
        node = queue.pop(0)
        if node in visited or not cmds.objExists(node):
            continue
        visited.add(node)
        if cmds.nodeType(node) not in allowed_types:
            continue
        graph_nodes.append(node)
        for source_plug in cmds.listConnections(
                node, source=True, destination=False, plugs=True) or []:
            source_node, source_attr = source_plug.rsplit(".", 1)
            if (cmds.objExists(source_node) and
                    cmds.nodeType(source_node) in allowed_types):
                queue.append(source_node)
            elif CORRECTIVE_VALUE_RE.match(source_attr):
                multiplier_plugs.append(source_plug)
    return (list(dict.fromkeys(graph_nodes)),
            list(dict.fromkeys(multiplier_plugs)))


def delete_surface_corrective(driver):
    """Deletes one surface driver, its skin joint, graph and private attrs."""
    if not (cmds.objExists(driver) and cmds.attributeQuery(
            "isQuaternionSurfaceDriver", node=driver, exists=True)):
        raise RuntimeError(
            "{} 不是自动表面修型驱动骨骼。".format(_short(driver)))
    graph_nodes, multiplier_plugs = _surface_driver_move_graph(driver)
    skin_joints = _skin_joints_for_driver(driver)
    constraint_nodes = []
    for skin in skin_joints:
        for constraint_type in ("parentConstraint", "scaleConstraint"):
            constraint_nodes.extend(cmds.listConnections(
                skin, source=True, destination=False,
                type=constraint_type) or [])
    delete_nodes = list(dict.fromkeys(
        constraint_nodes + skin_joints + graph_nodes + [driver]))
    existing = [node for node in delete_nodes if cmds.objExists(node)]
    if existing:
        cmds.delete(existing)

    removed_attrs = []
    for plug in multiplier_plugs:
        node, attribute = plug.rsplit(".", 1)
        if (cmds.objExists(node) and
                cmds.attributeQuery(attribute, node=node, exists=True) and
                not (cmds.listConnections(
                    plug, source=True, destination=True) or [])):
            cmds.deleteAttr(plug)
            removed_attrs.append(plug)
    return skin_joints, graph_nodes, removed_attrs


def _corrective_root_graph_nodes(root, buffer):
    """Collects the private quaternion nodes and constraints of one root."""
    allowed_types = {"eulerToQuat", "quatInvert", "quatSlerp", "quatToEuler"}
    queue = []
    for plug in [root + ".rotate"] + [root + ".rotate" + axis for axis in "XYZ"]:
        queue.extend(cmds.listConnections(
            plug, source=True, destination=False) or [])
    graph_nodes = []
    visited = set()
    while queue:
        node = queue.pop(0)
        if node in visited or not cmds.objExists(node):
            continue
        visited.add(node)
        if cmds.nodeType(node) not in allowed_types:
            continue
        graph_nodes.append(node)
        queue.extend(cmds.listConnections(
            node, source=True, destination=False) or [])

    constraint_nodes = []
    driven_nodes = [root]
    if buffer:
        driven_nodes.append(buffer)
    skin_roots = _skin_joints_for_driver(root)
    driven_nodes.extend(skin_roots)
    for driven in driven_nodes:
        for constraint_type in (
                "pointConstraint", "orientConstraint", "scaleConstraint",
                "parentConstraint"):
            constraint_nodes.extend(cmds.listConnections(
                driven, source=True, destination=False,
                type=constraint_type) or [])
    return (list(dict.fromkeys(graph_nodes + constraint_nodes)),
            skin_roots)


def delete_corrective_root(root):
    """Deletes a complete corrective root and every automatic/manual child."""
    if not (cmds.objExists(root) and cmds.attributeQuery(
            "isQuaternionCorrectiveRoot", node=root, exists=True)):
        raise RuntimeError(
            "{} 不是自动修型父级骨骼。".format(_short(root)))

    manual_parents = _manual_driver_parents_for_root(root)
    surface_drivers = _surface_drivers_under_root(root)
    deleted_drivers = []
    skin_nodes = []
    graph_nodes = []
    removed_attrs = []
    for driver in surface_drivers:
        skins, private_graph, attributes = delete_surface_corrective(driver)
        deleted_drivers.append(driver)
        skin_nodes.extend(skins)
        graph_nodes.extend(private_graph)
        removed_attrs.extend(attributes)

    source = None
    if cmds.attributeQuery("qcQuaternionSource", node=root, exists=True):
        source = cmds.getAttr(root + ".qcQuaternionSource")
    parents = cmds.listRelatives(root, parent=True, fullPath=True) or []
    buffer = parents[0] if parents else None
    if buffer and not re.search(
            r"_QC_Root_QuatBlend_GRP(?:\d+)?$", _short(buffer)):
        buffer = None

    root_graph, root_skin_nodes = _corrective_root_graph_nodes(root, buffer)
    skin_nodes.extend(root_skin_nodes)
    graph_nodes.extend(root_graph)
    manual_constraints = []
    for manual_parent in manual_parents:
        for constraint_type in (
                "pointConstraint", "orientConstraint", "scaleConstraint"):
            manual_constraints.extend(cmds.listConnections(
                manual_parent, source=True, destination=False,
                type=constraint_type) or [])
    graph_nodes.extend(manual_constraints)
    root_containers = [root] + manual_parents
    if buffer:
        root_containers.append(buffer)
    delete_nodes = list(dict.fromkeys(
        root_graph + manual_constraints +
        root_skin_nodes + root_containers))
    existing = [node for node in delete_nodes if cmds.objExists(node)]
    if existing:
        cmds.delete(existing)

    weight_plug = "{}.quatCorrectiveWeight".format(source) if source else None
    if (weight_plug and cmds.objExists(weight_plug) and
            not (cmds.listConnections(
                weight_plug, source=True, destination=True) or [])):
        cmds.deleteAttr(weight_plug)
        removed_attrs.append(weight_plug)
    graph_nodes.extend(root_containers)
    return (deleted_drivers, list(dict.fromkeys(skin_nodes)),
            list(dict.fromkeys(graph_nodes)),
            list(dict.fromkeys(removed_attrs)))


def add_surface_move_axis(driver, axis, base_distance):
    """Adds a missing local movement axis to an existing automatic driver."""
    if axis not in ("X", "Y", "Z"):
        raise RuntimeError("{} 不是有效的附加位移轴。".format(axis))
    if abs(base_distance) < 1e-8:
        raise RuntimeError("附加轴每度基础位移不能为 0。")
    if not cmds.attributeQuery(
            "isQuaternionSurfaceDriver", node=driver, exists=True):
        raise RuntimeError("{} 不是自动生成的表面修型驱动骨骼。".format(driver))

    sum_sources = cmds.listConnections(
        driver + ".translate", source=True, destination=False, plugs=True) or []
    if not sum_sources:
        raise RuntimeError("{} 的位移链接当前已断开，请先重新连接。".format(_short(driver)))
    translate_sum = sum_sources[0].rsplit(".", 1)[0]
    if cmds.nodeType(translate_sum) != "plusMinusAverage":
        raise RuntimeError("{} 的位移汇总节点无效。".format(_short(driver)))

    main_vectors = cmds.listConnections(
        translate_sum + ".input3D[1]", source=True, destination=False) or []
    if not main_vectors:
        raise RuntimeError("{} 找不到主位移向量。".format(_short(driver)))
    main_vector = main_vectors[0]
    distance_sources = cmds.listConnections(
        main_vector + ".input2X", source=True, destination=False, plugs=True) or []
    if not distance_sources:
        raise RuntimeError("{} 找不到主旋转位移距离。".format(_short(driver)))
    main_distance = distance_sources[0].rsplit(".", 1)[0]

    multiplier_sources = cmds.listConnections(
        main_distance + ".input2", source=True, destination=False, plugs=True) or []
    if not multiplier_sources:
        raise RuntimeError("{} 找不到源关节位移倍率属性。".format(_short(driver)))
    source_plug = multiplier_sources[0]
    source, main_attribute = source_plug.rsplit(".", 1)
    match = re.match(r"^qc((?:Pos|Neg)[XYZ])MoveMultiplier$", main_attribute)
    label = match.group(1) if match else None
    if not label:
        # Older/custom graphs may use a generic multiplier attribute.  Their
        # generated driver name still carries the Pos/Neg surface label.
        name_match = (re.search(
            r"_QC_((?:Pos|Neg)[XYZ])(?:_|$)", _short(driver)) or
            re.search(
                r"_QC_DRV_JNT_((?:Pos|Neg)[XYZ])(?:_|$)",
                _short(driver)))
        label = name_match.group(1) if name_match else None
    if not label:
        # Final fallback: infer the primary signed axis from the existing move
        # vector.  This also supports renamed driver joints.
        vector_value = cmds.getAttr(main_vector + ".input1")[0]
        axis_index = max(range(3), key=lambda index: abs(vector_value[index]))
        if abs(vector_value[axis_index]) > 1e-8:
            side = "Pos" if vector_value[axis_index] >= 0.0 else "Neg"
            label = side + "XYZ"[axis_index]
    if not label:
        raise RuntimeError(
            "{} 的主位移方向无法识别（倍率属性：{}）。".format(
                _short(driver), main_attribute))
    primary_axis = label[-1]
    if axis == primary_axis:
        raise RuntimeError("{} 已使用 {} 作为主位移轴。".format(_short(driver), axis))

    extra_attribute = _secondary_multiplier_name(label, axis)
    if cmds.attributeQuery(extra_attribute, node=source, exists=True):
        raise RuntimeError("{} 已有 {} 附加位移轴。".format(_short(driver), axis))

    angle_sources = cmds.listConnections(
        main_distance + ".input1", source=True, destination=False, plugs=True) or []
    if not angle_sources:
        raise RuntimeError("{} 找不到角度换算节点。".format(_short(driver)))
    main_angle_node = angle_sources[0].rsplit(".", 1)[0]
    bend_sources = cmds.listConnections(
        main_angle_node + ".input1", source=True, destination=False, plugs=True) or []
    if not bend_sources:
        raise RuntimeError("{} 找不到弯曲角度输入。".format(_short(driver)))

    multiplier_attr = _ensure_attr(
        source, extra_attribute, 0.0, -10.0, 10.0)
    prefix = "{}_QC_{}Secondary{}".format(_short(source), label, axis)
    angle_to_distance = cmds.createNode(
        "multDoubleLinear", name=prefix + "_AngleToDistance")
    cmds.setAttr(angle_to_distance + ".input2", base_distance)
    _connect(bend_sources[0], angle_to_distance + ".input1")
    distance = cmds.createNode(
        "multDoubleLinear", name=prefix + "_RotationMoveDistance")
    _connect(angle_to_distance + ".output", distance + ".input1")
    _connect(multiplier_attr, distance + ".input2")

    vector = cmds.createNode(
        "multiplyDivide", name=prefix + "_RotationMoveVector")
    _configure_move_vector(vector, axis)
    _connect_distance_to_vector(distance, vector)
    indices = cmds.getAttr(translate_sum + ".input3D", multiIndices=True) or [0, 1]
    slot = max(indices) + 1
    _connect(vector + ".output", "{}.input3D[{}]".format(translate_sum, slot))
    recorded_axes = list(_recorded_move_axes(driver)) or [primary_axis]
    if axis not in recorded_axes:
        recorded_axes.append(axis)
    _record_axis_metadata(
        driver,
        _recorded_rotation_axis(driver) or _upstream_rotation_axis(main_vector),
        primary_axis, recorded_axes)
    input_plug = _movement_branch_input(
        source, extra_attribute, driver, axis)
    if input_plug:
        input_node, input_channel = input_plug.rsplit(".", 1)
        _record_branch_input(
            driver, axis, input_node, input_channel)
    return source, multiplier_attr, (angle_to_distance, distance, vector)


def _surface_driver_existing_move_axes(driver):
    """Returns every movement axis already present on an automatic driver."""
    axes = list(_recorded_move_axes(driver))
    _graph_nodes, multiplier_plugs = _surface_driver_move_graph(driver)
    for plug in multiplier_plugs:
        attribute = plug.rsplit(".", 1)[-1]
        match = re.match(
            r"^qc(?:Pos|Neg)([XYZ])Move(?:([XYZ]))?Multiplier$",
            attribute)
        if not match:
            continue
        axis = match.group(2) or match.group(1)
        if axis not in axes:
            axes.append(axis)
    if not axes:
        label = _surface_label_from_driver(driver)
        if label:
            axes.append(label[-1])
    return tuple(axes)


def _surface_driver_secondary_move_axes(driver):
    """Returns removable axes while always protecting the primary axis."""
    primary_axis = _recorded_primary_move_axis(driver)
    if not primary_axis:
        label = _surface_label_from_driver(driver)
        primary_axis = label[-1] if label else None
    return tuple(
        axis for axis in _surface_driver_existing_move_axes(driver)
        if axis != primary_axis)


def remove_surface_move_axis(driver, axis):
    """Removes one secondary movement branch from an automatic driver."""
    if axis not in "XYZ":
        raise RuntimeError("{} 不是有效的位移方向。".format(axis))
    if not (cmds.objExists(driver) and cmds.attributeQuery(
            "isQuaternionSurfaceDriver", node=driver, exists=True)):
        raise RuntimeError("{} 不是自动生成的表面修型骨骼。".format(driver))

    label = _surface_label_from_driver(driver)
    primary_axis = _recorded_primary_move_axis(driver)
    if not primary_axis and label:
        primary_axis = label[-1]
    if not label or not primary_axis:
        raise RuntimeError("{} 无法识别主位移方向。".format(_short(driver)))
    if axis == primary_axis:
        raise RuntimeError(
            "{} 是主位移方向；如不再需要它，请删除整根修型骨骼。".format(axis))

    existing_axes = list(_surface_driver_existing_move_axes(driver))
    if axis not in existing_axes:
        raise RuntimeError("{} 没有位移 {} 驱动。".format(_short(driver), axis))

    attribute = _secondary_multiplier_name(label, axis)
    _graph_nodes, multiplier_plugs = _surface_driver_move_graph(driver)
    matching_plugs = [
        plug for plug in multiplier_plugs
        if plug.rsplit(".", 1)[-1] == attribute]
    if len(matching_plugs) != 1:
        raise RuntimeError(
            "{} 找不到唯一的位移 {} 倍率分支。".format(_short(driver), axis))
    multiplier_plug = matching_plugs[0]
    source, _attribute = multiplier_plug.rsplit(".", 1)
    distance_node, angle_node = _movement_branch_nodes(
        source, attribute, driver, axis)
    if not distance_node or not angle_node:
        raise RuntimeError(
            "{} 的位移 {} 计算分支不完整，未执行删除。".format(
                _short(driver), axis))

    vector_node = None
    for candidate in cmds.listConnections(
            distance_node, source=False, destination=True,
            type="multiplyDivide") or []:
        if (_vector_node_axis(candidate) == axis and
                _branch_reaches_target(candidate, driver)):
            vector_node = candidate
            break
    if not vector_node:
        raise RuntimeError(
            "{} 找不到位移 {} 输出向量，未执行删除。".format(
                _short(driver), axis))

    previous_inputs = [
        plug.rsplit(".", 1)[0] for plug in cmds.listConnections(
            angle_node + ".input1", source=True,
            destination=False, plugs=True) or []]
    cmds.delete([vector_node, distance_node, angle_node])
    for input_node in previous_inputs:
        _delete_orphaned_channel_node(input_node)

    if (cmds.objExists(source) and
            cmds.attributeQuery(attribute, node=source, exists=True) and
            not (cmds.listConnections(
                multiplier_plug, source=True, destination=True) or [])):
        cmds.deleteAttr(multiplier_plug)

    for metadata_attr in (
            "qcMove{}Source".format(axis),
            "qcMove{}SourceChannel".format(axis)):
        if cmds.attributeQuery(metadata_attr, node=driver, exists=True):
            metadata_plug = "{}.{}".format(driver, metadata_attr)
            try:
                cmds.setAttr(metadata_plug, lock=False)
            except RuntimeError:
                pass
            cmds.deleteAttr(metadata_plug)

    remaining_axes = [
        move_axis for move_axis in existing_axes if move_axis != axis]
    _record_axis_metadata(
        driver, _recorded_rotation_axis(driver), primary_axis,
        remaining_axes)
    driver = repair_corrective_bone_names(
        driver, source, label, move_axes=remaining_axes)
    return driver, source, attribute


def _stored_source_attr(driver):
    attribute = "qcStoredTranslateSource"
    if not cmds.attributeQuery(attribute, node=driver, exists=True):
        cmds.addAttr(driver, longName=attribute, dataType="string")
    return driver + "." + attribute


def disconnect_surface_translate(driver):
    """Temporarily releases a surface driver's local translate for manual placement."""
    sources = cmds.listConnections(driver + ".translate", source=True, destination=False, plugs=True) or []
    if not sources:
        raise RuntimeError("{} 没有可断开的位移链接。".format(driver))
    source = sources[0]
    cmds.setAttr(_stored_source_attr(driver), source, type="string")
    cmds.disconnectAttr(source, driver + ".translate")


def reconnect_surface_translate(driver, update_direction=False):
    """Uses the current local position as the new rest offset, then restores the movement graph."""
    source = cmds.getAttr(_stored_source_attr(driver))
    if not source or "." not in source:
        raise RuntimeError("{} 没有已保存的位移链接。请先点击“断开位移链接”。".format(driver))
    sum_node = source.rsplit(".", 1)[0]
    if not cmds.objExists(sum_node) or cmds.nodeType(sum_node) != "plusMinusAverage":
        raise RuntimeError("{} 的已保存位移链接无效。".format(driver))

    desired = cmds.getAttr(driver + ".translate")[0]
    vector_nodes = cmds.listConnections(sum_node + ".input3D[1]", source=True, destination=False) or []
    vector_node = vector_nodes[0] if vector_nodes else None

    # A single "new direction" cannot represent a deliberately multi-axis
    # graph.  Preserve all selected local axes and only rebase its rest offset.
    extra_slots = [index for index in (cmds.getAttr(sum_node + ".input3D", multiIndices=True) or [])
                   if index > 1 and cmds.listConnections(
                       "{}.input3D[{}]".format(sum_node, index), source=True, destination=False)]
    _migrate_axis_metadata(driver, sum_node, vector_node, extra_slots)
    recorded_axes = _recorded_move_axes(driver)
    if update_direction and (len(recorded_axes) > 1 or extra_slots):
        update_direction = False
    if (not update_direction and vector_node and
            cmds.nodeType(vector_node) == "multiplyDivide"):
        primary_axis = (_recorded_primary_move_axis(driver) or
                        _vector_node_axis(vector_node))
        if primary_axis:
            surface_label = _surface_label_from_driver(driver)
            if surface_label:
                primary_sign = (1.0 if surface_label.startswith("Pos")
                                else -1.0)
            else:
                current_direction = cmds.getAttr(vector_node + ".input1")[0]
                component = current_direction["XYZ".index(primary_axis)]
                primary_sign = -1.0 if component < 0.0 else 1.0
            _configure_move_vector(
                vector_node, primary_axis, primary_sign)
        for slot in extra_slots:
            extra_vectors = cmds.listConnections(
                "{}.input3D[{}]".format(sum_node, slot),
                source=True, destination=False) or []
            if extra_vectors:
                extra_axis = _vector_node_axis(extra_vectors[0])
                if extra_axis:
                    _configure_move_vector(extra_vectors[0], extra_axis)

    # Re-evaluate the offsets after restoring recorded axes so reconnecting an
    # older, already-corrupted graph keeps the visible joint in place.
    primary_delta = cmds.getAttr(sum_node + ".input3D[1]")[0]
    total_delta = _translate_sum_movement(sum_node)
    base = tuple(desired[index] - total_delta[index] for index in range(3))
    if update_direction and vector_node and cmds.nodeType(vector_node) == "multiplyDivide":
        old_direction = cmds.getAttr(vector_node + ".input1")[0]
        signed_distance = sum(primary_delta[index] * old_direction[index] for index in range(3))
        desired_length = sum(value * value for value in desired) ** 0.5
        if desired_length > 1e-8:
            new_direction = tuple(value / desired_length for value in desired)
            cmds.setAttr(vector_node + ".input1", *new_direction, type="double3")
            secondary_delta = tuple(total_delta[index] - primary_delta[index] for index in range(3))
            base = tuple(desired[index] - secondary_delta[index] -
                         signed_distance * new_direction[index] for index in range(3))

    cmds.setAttr(sum_node + ".input3D[0]", *base, type="double3")
    _connect(source, driver + ".translate")
    cmds.setAttr(_stored_source_attr(driver), "", type="string")


def create_for_joint(source, mesh, driver_parent, settings):
    """Creates one complete driver + skin-corrective layer. Returns created nodes."""
    _validate_move_settings(settings)
    if cmds.nodeType(source) != "joint":
        raise RuntimeError("{} 不是关节。".format(source))
    short = _short(source)
    root_name = short + "_QC_DRV_ROOT_JNT"
    if cmds.objExists(root_name):
        raise RuntimeError("{} 已存在，请先删除该关节之前创建的修型系统。".format(root_name))

    surface_axes = _creation_surface_axes(settings)
    base = cmds.xform(source, query=True, worldSpace=True, translation=True)
    probe_distance = settings["probe_distance"]
    surface_positions = {}
    # Resolve every requested surface hit before creating nodes.  A bad axis
    # therefore cannot leave a half-built multi-axis corrective system behind.
    for surface_axis in surface_axes:
        free_axis = _source_axis_world(source, surface_axis)
        surface_positions[surface_axis] = (
            _ray_mesh_intersection(
                mesh, base, free_axis, probe_distance),
            _ray_mesh_intersection(
                mesh, base, -free_axis, probe_distance))

    weight_attr = _quaternion_weight_attr(source, settings.get("weight", 0.5))
    root = cmds.createNode("joint", name=root_name, parent=driver_parent)
    _match_joint_axes_to_source(root, source)
    cmds.setAttr(root + ".rotateOrder", cmds.getAttr(source + ".rotateOrder"))
    cmds.setAttr(
        root + ".radius",
        cmds.getAttr(source + ".radius") + CORRECTIVE_RADIUS_OFFSET)
    cmds.setAttr(root + ".segmentScaleCompensate", 0)
    cmds.addAttr(root, longName="isQuaternionCorrectiveRoot", attributeType="bool", defaultValue=True)
    cmds.setAttr(root + ".isQuaternionCorrectiveRoot", lock=True)
    _record_corrective_root_source(root, source)
    cmds.pointConstraint(source, root, maintainOffset=False, name=short + "_QC_Root_pointConstraint")
    cmds.scaleConstraint(source, root, maintainOffset=False, name=short + "_QC_Root_scaleConstraint")
    nodes = [root] + list(_make_root_quaternion_network(
        source, root, settings["inverse"], weight_attr))
    positive_rotation = _positive_rotation_for(source, settings)
    # Skin corrective joints live directly under their selected source joint,
    # keeping every generated set inside the artist's existing joint hierarchy.
    skin_root = _make_skin_joint(
        root, source, short + "_QC_SKIN_ROOT_JNT")
    nodes.append(skin_root)

    for surface_axis in surface_axes:
        plus_surface, minus_surface = surface_positions[surface_axis]
        plus_label = "Pos" + surface_axis
        minus_label = "Neg" + surface_axis
        extra_axes = _additional_move_axes(settings, surface_axis)
        plus_attr = _ensure_attr(
            source, "qc{}MoveMultiplier".format(plus_label),
            0.0, -10.0, 10.0)
        minus_attr = _ensure_attr(
            source, "qc{}MoveMultiplier".format(minus_label),
            0.0, -10.0, 10.0)
        plus_distance, plus_move_nodes = _make_rotation_distance(
            source, plus_label, settings["rotation_axis"], positive_rotation,
            settings["base_distance"], plus_attr)
        minus_distance, minus_move_nodes = _make_rotation_distance(
            source, minus_label, settings["rotation_axis"], positive_rotation,
            settings["base_distance"], minus_attr)

        plus_extra_moves, minus_extra_moves = [], []
        plus_extra_nodes, minus_extra_nodes = [], []
        for extra_axis in extra_axes:
            plus_extra_attr = _ensure_attr(
                source, _secondary_multiplier_name(plus_label, extra_axis),
                0.0, -10.0, 10.0)
            minus_extra_attr = _ensure_attr(
                source, _secondary_multiplier_name(minus_label, extra_axis),
                0.0, -10.0, 10.0)
            plus_extra_distance, plus_nodes_for_axis = _make_rotation_distance(
                source, plus_label + "Secondary" + extra_axis,
                settings["rotation_axis"], positive_rotation,
                _second_distance_from_settings(settings), plus_extra_attr)
            minus_extra_distance, minus_nodes_for_axis = _make_rotation_distance(
                source, minus_label + "Secondary" + extra_axis,
                settings["rotation_axis"], positive_rotation,
                _second_distance_from_settings(settings), minus_extra_attr)
            plus_extra_moves.append((extra_axis, plus_extra_distance))
            minus_extra_moves.append((extra_axis, minus_extra_distance))
            plus_extra_nodes.extend(plus_nodes_for_axis)
            minus_extra_nodes.extend(minus_nodes_for_axis)

        plus, plus_nodes = _make_surface_driver(
            source, root, plus_label, plus_surface, plus_distance,
            additional_movements=plus_extra_moves)
        minus, minus_nodes = _make_surface_driver(
            source, root, minus_label, minus_surface, minus_distance,
            additional_movements=minus_extra_moves)
        move_axes = [surface_axis] + extra_axes
        _record_axis_metadata(
            plus, settings["rotation_axis"], surface_axis, move_axes)
        _record_axis_metadata(
            minus, settings["rotation_axis"], surface_axis, move_axes)
        input_channel = "rotate" + settings["rotation_axis"]
        for move_axis in move_axes:
            _record_branch_input(plus, move_axis, source, input_channel)
            _record_branch_input(minus, move_axis, source, input_channel)
        _plus_driver_name, plus_skin_name = _automatic_corrective_names(
            source, plus_label, move_axes)
        _minus_driver_name, minus_skin_name = _automatic_corrective_names(
            source, minus_label, move_axes)
        skin_plus = _make_skin_joint(
            plus, source, plus_skin_name)
        skin_minus = _make_skin_joint(
            minus, source, minus_skin_name)
        nodes.extend(
            plus_move_nodes + minus_move_nodes +
            plus_extra_nodes + minus_extra_nodes +
            [plus, minus, skin_plus, skin_minus] +
            list(plus_nodes) + list(minus_nodes))
    return nodes


def _maya_main_window():
    pointer = omui.MQtUtil.mainWindow()
    return wrapInstance(int(pointer), QtWidgets.QWidget) if pointer else None


class _UI(QtWidgets.QDialog):
    """Maya 2025 / PySide6 interface for the corrective rig tools."""

    def __init__(self, parent=None):
        super(_UI, self).__init__(parent or _maya_main_window())
        self._driver_bindings = []
        self._slider_undo_open = False
        self._display_layers_initialized = False
        self._display_mode = None
        self._driver_display_count = 0
        self._skin_display_count = 0
        self._active_driver_context_menu = None
        self._active_driver_context_item = None
        self._maya_callback_ids = []
        self._manager_history_refresh_pending = False
        self._last_axis_delete_source = None
        self.setObjectName(WINDOW)
        self.setWindowTitle("骨骼修型")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowType.Window)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(600, 650)
        self.resize(640, 760)
        self._build_ui()
        self._apply_style()
        self.driver_display_button.setChecked(True)
        self._display_mode = "driver"
        self._install_maya_callbacks()
        QtCore.QTimer.singleShot(0, self._initialize_default_display_mode)

    @staticmethod
    def _combo(items, current=0):
        widget = QtWidgets.QComboBox()
        widget.addItems(items)
        widget.setCurrentIndex(current)
        return widget

    @staticmethod
    def _combo_data(items, current=0):
        """Builds a readable combo while keeping compact rig values as data."""
        widget = QtWidgets.QComboBox()
        for label, value in items:
            widget.addItem(label, value)
        widget.setCurrentIndex(current)
        return widget

    @staticmethod
    def _number(value, minimum=-1000.0, maximum=1000.0, step=0.01):
        widget = QtWidgets.QDoubleSpinBox()
        widget.setDecimals(4)
        widget.setRange(minimum, maximum)
        widget.setSingleStep(step)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        return widget

    @staticmethod
    def _hint(text):
        label = QtWidgets.QLabel(text)
        label.setObjectName("hintLabel")
        label.setWordWrap(True)
        return label

    @staticmethod
    def _group(title):
        group = QtWidgets.QGroupBox(title)
        layout = QtWidgets.QVBoxLayout(group)
        layout.setContentsMargins(14, 16, 14, 14)
        layout.setSpacing(10)
        return group, layout

    @staticmethod
    def _form():
        layout = QtWidgets.QFormLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(8)
        layout.setLabelAlignment(
            QtCore.Qt.AlignmentFlag.AlignRight |
            QtCore.Qt.AlignmentFlag.AlignVCenter)
        layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        return layout

    @staticmethod
    def _page():
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        content = QtWidgets.QWidget()
        content.setObjectName("pageContent")
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        scroll.setWidget(content)
        return scroll, layout

    @staticmethod
    def _axis_checks():
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        checks = []
        for axis in "XYZ":
            checkbox = QtWidgets.QCheckBox(axis)
            checks.append(checkbox)
            layout.addWidget(checkbox)
        layout.addStretch(1)
        return container, checks

    def _selection_field(self, default_text, expected_type):
        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        field = QtWidgets.QLineEdit(default_text)
        field.setClearButtonEnabled(True)
        button = QtWidgets.QPushButton("读取选择")
        button.setFixedWidth(82)
        button.clicked.connect(
            lambda checked=False, edit=field, kind=expected_type:
            self._load_selected(edit, kind))
        layout.addWidget(field, 1)
        layout.addWidget(button)
        return container, field

    def _collapsible(self, title, expanded=False):
        container = QtWidgets.QFrame()
        container.setObjectName("collapsibleFrame")
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        button = QtWidgets.QToolButton()
        button.setObjectName("collapseButton")
        button.setText(title)
        button.setCheckable(True)
        button.setChecked(expanded)
        button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setArrowType(
            QtCore.Qt.ArrowType.DownArrow if expanded
            else QtCore.Qt.ArrowType.RightArrow)
        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(14, 4, 4, 4)
        content_layout.setSpacing(8)
        content.setVisible(expanded)

        def toggle(visible):
            content.setVisible(visible)
            button.setArrowType(
                QtCore.Qt.ArrowType.DownArrow if visible
                else QtCore.Qt.ArrowType.RightArrow)

        button.toggled.connect(toggle)
        layout.addWidget(button)
        layout.addWidget(content)
        return container, content_layout

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(14)

        self.delete_selected_button = QtWidgets.QPushButton(
            "删除所选\n骨骼及链接")
        self.delete_selected_button.setObjectName("dangerButton")
        self.delete_selected_button.setFixedSize(84, 48)
        self.delete_selected_button.setToolTip(
            "选择父级根骨骼时删除整套修型系统；选择 Pos/Neg 骨骼时"
            "只删除对应方向、蒙皮骨骼及相关驱动链接。")
        self.delete_selected_button.clicked.connect(
            self.delete_selected_correctives)

        display_switch = QtWidgets.QFrame()
        display_switch.setObjectName("boneDisplaySwitch")
        switch_layout = QtWidgets.QVBoxLayout(display_switch)
        switch_layout.setContentsMargins(8, 6, 8, 7)
        switch_layout.setSpacing(4)
        switch_label = QtWidgets.QLabel("骨骼显示")
        switch_label.setObjectName("switchLabel")
        switch_layout.addWidget(switch_label)
        switch_buttons = QtWidgets.QHBoxLayout()
        switch_buttons.setContentsMargins(0, 0, 0, 0)
        switch_buttons.setSpacing(0)
        self.driver_display_button = QtWidgets.QPushButton("驱动")
        self.driver_display_button.setObjectName("displayModeButton")
        self.driver_display_button.setCheckable(True)
        self.driver_display_button.setToolTip(
            "显示本工具生成的驱动骨骼，并隐藏蒙皮修型骨骼。")
        self.skin_display_button = QtWidgets.QPushButton("蒙皮")
        self.skin_display_button.setObjectName("displayModeButton")
        self.skin_display_button.setCheckable(True)
        self.skin_display_button.setToolTip(
            "显示本工具生成的蒙皮修型骨骼，并隐藏驱动骨骼。")
        self.display_button_group = QtWidgets.QButtonGroup(self)
        self.display_button_group.setExclusive(True)
        self.display_button_group.addButton(self.driver_display_button)
        self.display_button_group.addButton(self.skin_display_button)
        self.driver_display_button.clicked.connect(
            lambda: self._set_bone_display_mode("driver"))
        self.skin_display_button.clicked.connect(
            lambda: self._set_bone_display_mode("skin"))
        switch_buttons.addWidget(self.driver_display_button)
        switch_buttons.addWidget(self.skin_display_button)
        switch_layout.addLayout(switch_buttons)
        header.addWidget(display_switch)
        header.addStretch(1)
        header.addWidget(self.delete_selected_button)
        root.addLayout(header)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_create_page(), "创建")
        self.tabs.addTab(self._build_add_rebuild_page(), "新增 / 重建")
        self.tabs.addTab(self._build_driver_manager_page(), "驱动管理")
        root.addWidget(self.tabs, 1)

        self.status_frame = QtWidgets.QFrame()
        self.status_frame.setObjectName("statusFrame")
        status_layout = QtWidgets.QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(10, 8, 10, 8)
        self.status_label = QtWidgets.QLabel("准备就绪")
        self.status_label.setWordWrap(True)
        status_layout.addWidget(self.status_label)
        root.addWidget(self.status_frame)

    def _build_create_page(self):
        page, layout = self._page()
        object_group, object_layout = self._group("场景对象")
        object_form = self._form()
        row, self.mesh_field = self._selection_field("", "mesh")
        object_form.addRow("表面网格", row)
        row, self.driver_group_field = self._selection_field(
            "QuaternionCorrectiveDrivers_GRP", "transform")
        object_form.addRow("驱动骨骼组", row)
        object_layout.addLayout(object_form)
        layout.addWidget(object_group)

        settings_group, settings_layout = self._group("创建参数")
        settings_form = self._form()
        self.rotation_axis = self._combo(["X", "Y", "Z"], 2)
        self.rotation_sign = self._combo(["自动（Knee - / Elbow +）", "+", "-"])
        creation_axis_row, creation_axis_checks = self._axis_checks()
        (self.create_axis_x,
         self.create_axis_y,
         self.create_axis_z) = creation_axis_checks
        self.create_axis_y.setChecked(True)
        self.weight = self._number(0.5, -1.0, 1.0, 0.05)
        settings_form.addRow("旋转驱动轴", self.rotation_axis)
        settings_form.addRow("弯曲方向", self.rotation_sign)
        settings_form.addRow("关节方向", creation_axis_row)
        settings_form.addRow("四元数权重（默认 +0.5）", self.weight)
        settings_layout.addLayout(settings_form)

        advanced, advanced_layout = self._collapsible("高级设置", expanded=False)
        advanced_form = self._form()
        self.probe_distance = self._number(1000.0, 0.001, 100000.0, 10.0)
        self.probe_distance.setToolTip(
            "射线从源关节出发的最大搜索距离；没有碰到模型时可增大此值。")
        self.base_distance = self._number(0.02, -1000.0, 1000.0, 0.01)
        self.inverse_quaternion = QtWidgets.QCheckBox("启用")
        self.inverse_quaternion.setChecked(False)
        advanced_form.addRow("最大表面投射距离", self.probe_distance)
        advanced_form.addRow("主轴每度位移", self.base_distance)
        advanced_form.addRow("反向根骨骼四元数", self.inverse_quaternion)
        advanced_layout.addLayout(advanced_form)
        settings_layout.addWidget(advanced)
        layout.addWidget(settings_group)

        create_button = QtWidgets.QPushButton("创建")
        create_button.setObjectName("primaryButton")
        create_button.setMinimumHeight(40)
        create_button.clicked.connect(self.create)
        layout.addWidget(create_button)
        layout.addStretch(1)
        return page

    def _build_add_rebuild_page(self):
        page, layout = self._page()
        add_group, add_layout = self._group("新增驱动")
        selection_help = QtWidgets.QLabel(
            "先选重新摆放的骨骼，再加选负责旋转的源关节。建立后会自动整理驱动层级并创建蒙皮骨骼。")
        selection_help.setObjectName("instructionLabel")
        add_layout.addWidget(selection_help)

        add_form = self._form()
        self.manual_rotation_axis = self._combo_data([
            ("rotateX（绕本地 X 轴）", "X"),
            ("rotateY（绕本地 Y 轴）", "Y"),
            ("rotateZ（绕本地 Z 轴）", "Z"),
        ], 2)
        self.manual_rotation_axis.setToolTip(
            "观察驱动父级旋转时，Channel Box 中 rotateX / rotateY / rotateZ 哪个数值在变化。")
        self.manual_surface_axis = self._combo_data([
            ("自身 +X 轴", "X"),
            ("自身 +Y 轴", "Y"),
            ("自身 +Z 轴", "Z"),
        ], 1)
        self.manual_surface_axis.setToolTip(
            "连接前会记录新增修型骨骼当前朝向。旋转增量为正时沿所选正轴移动，"
            "旋转增量为负时自动沿反轴移动。")
        self.manual_base_distance = self._number(0.02, -1000.0, 1000.0, 0.01)
        self.manual_base_distance.setToolTip(
            "新增倍率默认是 0，因此建立后不会移动；在驱动管理中把倍率调到 1 时，"
            "例如每度位移 0.02、旋转 10°，骨骼约移动 0.2 个 Maya 单位。")
        self.manual_keep_original_parent = QtWidgets.QCheckBox(
            "保持原父级空间")
        self.manual_keep_original_parent.setChecked(False)
        self.manual_keep_original_parent.setToolTip(
            "勾选后只读取第二选择骨骼的旋转数值；新增修型骨骼的基础空间继续跟随"
            "第一选择骨骼创建前的父级，不会被旋转源关节整体带走。")
        add_form.addRow("父级旋转", self.manual_rotation_axis)
        add_form.addRow("移动方向", self.manual_surface_axis)
        add_form.addRow("每度位移", self.manual_base_distance)
        add_form.addRow("空间跟随", self.manual_keep_original_parent)
        add_layout.addLayout(add_form)

        manual_advanced, manual_advanced_layout = self._collapsible(
            "叠加其他移动方向", expanded=False)
        manual_advanced_form = self._form()
        manual_axis_row, manual_checks = self._axis_checks()
        (self.manual_extra_axis_x,
         self.manual_extra_axis_y,
         self.manual_extra_axis_z) = manual_checks
        self.manual_extra_axis_x.setText("自身 +X")
        self.manual_extra_axis_y.setText("自身 +Y")
        self.manual_extra_axis_z.setText("自身 +Z")
        self.manual_extra_distance = self._number(0.02, -1000.0, 1000.0, 0.01)
        manual_advanced_form.addRow("叠加轴", manual_axis_row)
        manual_advanced_form.addRow("每度位移", self.manual_extra_distance)
        manual_advanced_layout.addLayout(manual_advanced_form)
        add_layout.addWidget(manual_advanced)

        manual_button = QtWidgets.QPushButton("建立驱动")
        manual_button.setObjectName("primaryButton")
        manual_button.setMinimumHeight(38)
        manual_button.clicked.connect(self.create_manual_link)
        add_layout.addWidget(manual_button)
        layout.addWidget(add_group)

        extend_group, extend_layout = self._collapsible(
            "可选工具 · 补加自动修型位移轴", expanded=False)
        extend_form = self._form()
        extend_axis_row, extend_checks = self._axis_checks()
        (self.extend_axis_x,
         self.extend_axis_y,
         self.extend_axis_z) = extend_checks
        self.extend_axis_x.setText("源关节 X")
        self.extend_axis_y.setText("源关节 Y")
        self.extend_axis_z.setText("源关节 Z")
        self.extend_distance = self._number(0.02, -1000.0, 1000.0, 0.01)
        self.extend_distance.setToolTip(
            "新增属性的默认倍率为 0；这里设置旋转每变化 1° 时的基础位移。")
        extend_form.addRow("补加轴", extend_axis_row)
        extend_form.addRow("每度位移", self.extend_distance)
        extend_layout.addLayout(extend_form)
        extend_button = QtWidgets.QPushButton("补加到所选骨骼")
        extend_button.clicked.connect(self.add_axes_to_selected)
        extend_layout.addWidget(extend_button)
        layout.addWidget(extend_group)

        layout.addStretch(1)
        return page

    def _build_driver_manager_page(self):
        page = QtWidgets.QWidget()
        page.setObjectName("pageContent")
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        source_row = QtWidgets.QHBoxLayout()
        source_row.setSpacing(8)
        load_selected = QtWidgets.QPushButton("所选关节")
        load_selected.clicked.connect(lambda: self._load_driver_manager(False))
        scan_scene = QtWidgets.QPushButton("扫描场景")
        scan_scene.clicked.connect(lambda: self._load_driver_manager(True))
        disconnect_button = QtWidgets.QPushButton("断开位移")
        disconnect_button.setFixedWidth(90)
        disconnect_button.setToolTip(
            "断开所选自动修型骨骼的位移链接，以便在视图中调整位置。")
        disconnect_button.clicked.connect(self.disconnect_selected)
        self.update_direction = QtWidgets.QCheckBox("重算方向")
        self.update_direction.setChecked(False)
        self.update_direction.setFixedWidth(90)
        self.update_direction.setToolTip(
            "默认关闭：保存新位置但保留创建时记录的移动轴。"
            "勾选后，根据当前位置重新计算单轴骨骼的移动方向；"
            "多轴骨骼始终保留原有的全部移动轴。")
        reconnect_button = QtWidgets.QPushButton("保存并重连")
        reconnect_button.setObjectName("primaryButton")
        reconnect_button.setFixedWidth(140)
        reconnect_button.setToolTip(
            "保存所选骨骼当前位置，并恢复位移驱动。")
        reconnect_button.clicked.connect(self.reconnect_selected)
        refresh_values = QtWidgets.QPushButton("刷新数值")
        refresh_values.clicked.connect(self._refresh_driver_values)
        source_row.addWidget(load_selected)
        source_row.addWidget(scan_scene)
        source_row.addWidget(disconnect_button)
        source_row.addWidget(reconnect_button)
        source_row.addWidget(self.update_direction)
        source_row.addWidget(refresh_values)
        layout.addLayout(source_row)

        filter_row = QtWidgets.QHBoxLayout()
        self.driver_search = QtWidgets.QLineEdit()
        self.driver_search.setPlaceholderText("搜索关节、修型骨骼或属性名…")
        self.driver_search.setClearButtonEnabled(True)
        self.driver_search.textChanged.connect(self._filter_driver_items)
        self.driver_filter = QtWidgets.QComboBox()
        self.driver_filter.addItems(["全部驱动", "仅显示已调整"])
        self.driver_filter.currentIndexChanged.connect(self._filter_driver_items)
        expand_all = QtWidgets.QPushButton("展开")
        expand_all.setFixedWidth(58)
        expand_all.clicked.connect(self.driver_tree_expand_all)
        collapse_all = QtWidgets.QPushButton("折叠")
        collapse_all.setFixedWidth(58)
        collapse_all.clicked.connect(self.driver_tree_collapse_all)
        filter_row.addWidget(self.driver_search, 1)
        filter_row.addWidget(self.driver_filter)
        filter_row.addWidget(expand_all)
        filter_row.addWidget(collapse_all)
        layout.addLayout(filter_row)

        self.driver_tree = QtWidgets.QTreeWidget()
        self.driver_tree.setObjectName("driverTree")
        self.driver_tree.setColumnCount(2)
        self.driver_tree.setHeaderLabels(["关节 / 驱动项目", "倍率调节"])
        self.driver_tree.setAlternatingRowColors(True)
        self.driver_tree.setRootIsDecorated(True)
        self.driver_tree.setUniformRowHeights(False)
        self.driver_tree.header().setSectionResizeMode(
            0, QtWidgets.QHeaderView.ResizeMode.Stretch)
        self.driver_tree.header().setSectionResizeMode(
            1, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.driver_tree.itemDoubleClicked.connect(self._select_driver_tree_item)
        self.driver_tree.setContextMenuPolicy(
            QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.driver_tree.customContextMenuRequested.connect(
            self._schedule_driver_channel_menu)
        layout.addWidget(self.driver_tree, 1)

        footer = QtWidgets.QHBoxLayout()
        self.driver_summary = QtWidgets.QLabel("尚未读取驱动")
        self.driver_summary.setObjectName("hintLabel")
        reset_visible = QtWidgets.QPushButton("重置当前可见项")
        reset_visible.clicked.connect(self._reset_visible_driver_values)
        footer.addWidget(self.driver_summary)
        footer.addStretch(1)
        footer.addWidget(reset_visible)
        layout.addLayout(footer)
        return page

    def driver_tree_expand_all(self):
        self.driver_tree.expandAll()

    def driver_tree_collapse_all(self):
        self.driver_tree.collapseAll()

    @staticmethod
    def _attribute_range(node, attribute, current):
        minimum = cmds.attributeQuery(attribute, node=node, minimum=True)
        maximum = cmds.attributeQuery(attribute, node=node, maximum=True)
        minimum = minimum[0] if minimum else min(-10.0, current)
        maximum = maximum[0] if maximum else max(10.0, current)
        if abs(maximum - minimum) < 1e-8:
            maximum = minimum + 1.0
        return float(minimum), float(maximum)

    @staticmethod
    def _slider_position(value, minimum, maximum):
        ratio = (float(value) - minimum) / (maximum - minimum)
        return int(round(max(0.0, min(1.0, ratio)) * 2000.0))

    @staticmethod
    def _slider_value(position, minimum, maximum):
        return minimum + (maximum - minimum) * (float(position) / 2000.0)

    def _driver_value_control(self, node, attribute, item):
        plug = "{}.{}".format(node, attribute)
        current = float(cmds.getAttr(plug))
        defaults = cmds.attributeQuery(attribute, node=node, listDefault=True)
        default = float(defaults[0]) if defaults else 0.0
        minimum, maximum = self._attribute_range(node, attribute, current)

        container = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(container)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(5)

        slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        slider.setRange(0, 2000)
        slider.setMinimumWidth(145)
        slider.setToolTip(
            "调整旋转/位移输入产生修型位移的倍率；输入处于中性值时不会移动骨骼。")
        slider.setValue(self._slider_position(current, minimum, maximum))
        spin = self._number(current, minimum, maximum, 0.05)
        spin.setFixedWidth(82)
        zero_button = QtWidgets.QPushButton("0")
        zero_button.setToolTip("临时关闭此驱动项")
        zero_button.setFixedSize(28, 26)
        reset_button = QtWidgets.QPushButton("↺")
        reset_button.setToolTip("恢复生成时的默认值：{}".format(default))
        reset_button.setFixedSize(28, 26)

        layout.addWidget(slider, 1)
        layout.addWidget(spin)
        layout.addWidget(zero_button)
        layout.addWidget(reset_button)

        binding = {
            "node": node,
            "attribute": attribute,
            "plug": plug,
            "item": item,
            "slider": slider,
            "spin": spin,
            "minimum": minimum,
            "maximum": maximum,
            "default": default,
            "neutral_checked": False,
        }
        slider.valueChanged.connect(
            lambda position, data=binding: self._driver_slider_changed(data, position))
        slider.sliderPressed.connect(self._begin_slider_edit)
        slider.sliderReleased.connect(self._end_slider_edit)
        spin.valueChanged.connect(
            lambda value, data=binding: self._driver_spin_changed(data, value))
        zero_button.clicked.connect(
            lambda checked=False, data=binding: self._apply_driver_value(data, 0.0, True))
        reset_button.clicked.connect(
            lambda checked=False, data=binding: self._apply_driver_value(
                data, data["default"], True))

        editable = bool(cmds.getAttr(plug, settable=True))
        container.setEnabled(editable)
        if not editable:
            item.setToolTip(0, plug + "\n该属性已有输入连接或已锁定，不能在此编辑。")
        self._driver_bindings.append(binding)
        return container

    def _driver_manager_sources(self):
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        sources = []
        for index in range(self.driver_tree.topLevelItemCount()):
            source = self.driver_tree.topLevelItem(index).data(0, role)
            if source and cmds.objExists(source) and source not in sources:
                sources.append(source)
        return sources

    def _capture_driver_tree_state(self):
        """Captures expansion, focus and scroll before rebuilding the tree."""
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        state = {
            "had_items": self.driver_tree.topLevelItemCount() > 0,
            "expanded_sources": [],
            "current": None,
            "vertical_scroll": self.driver_tree.verticalScrollBar().value(),
            "horizontal_scroll": self.driver_tree.horizontalScrollBar().value(),
        }
        for index in range(self.driver_tree.topLevelItemCount()):
            top = self.driver_tree.topLevelItem(index)
            source = top.data(0, role)
            if source and top.isExpanded():
                state["expanded_sources"].append(source)
        current = self.driver_tree.currentItem()
        if current is not None:
            state["current"] = tuple(
                current.data(0, role + offset) or ""
                for offset in range(5))
        return state

    @staticmethod
    def _driver_state_node_matches(first, second):
        if not first or not second:
            return first == second
        if cmds.objExists(first) and cmds.objExists(second):
            return _same_node(first, second)
        return first == second or _short(first) == _short(second)

    def _restore_driver_tree_state(self, state):
        """Restores the user's exact working area after a manager refresh."""
        if not state or not state.get("had_items"):
            return
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        expanded_sources = state.get("expanded_sources", [])
        current_state = state.get("current")
        restored_current = None

        for top_index in range(self.driver_tree.topLevelItemCount()):
            top = self.driver_tree.topLevelItem(top_index)
            source = top.data(0, role) or ""
            top.setExpanded(any(
                self._driver_state_node_matches(source, previous)
                for previous in expanded_sources))
            if not current_state:
                continue
            for row_index in range(top.childCount()):
                row = top.child(row_index)
                row_state = tuple(
                    row.data(0, role + offset) or ""
                    for offset in range(5))
                same_source = self._driver_state_node_matches(
                    row_state[0], current_state[0])
                same_attribute = row_state[1] == current_state[1]
                same_axis = row_state[3] == current_state[3]
                # The target joint can be renamed when an axis is deleted or
                # restored.  Source + multiplier attribute + output axis is
                # the stable identity of one manager row.
                if same_source and same_attribute and same_axis:
                    restored_current = row
                    break
            if restored_current:
                break

        if restored_current and not restored_current.isHidden():
            self.driver_tree.setCurrentItem(restored_current)
        vertical = state.get("vertical_scroll", 0)
        horizontal = state.get("horizontal_scroll", 0)
        QtCore.QTimer.singleShot(
            0, lambda value=vertical:
            self.driver_tree.verticalScrollBar().setValue(value))
        QtCore.QTimer.singleShot(
            0, lambda value=horizontal:
            self.driver_tree.horizontalScrollBar().setValue(value))

    def _load_driver_manager(self, scan_scene=False, nodes_override=None):
        tree_state = self._capture_driver_tree_state()
        if nodes_override is not None:
            nodes = [node for node in nodes_override if cmds.objExists(node)]
        elif scan_scene:
            nodes = (cmds.ls(type="joint", long=True) or [])
            nodes += (cmds.ls(type="transform", long=True) or [])
        else:
            nodes = cmds.ls(selection=True, long=True) or []
            if not nodes:
                message = "请先选择创建修型系统时使用的源关节，或使用“扫描场景”。"
                cmds.warning(message)
                self._show_status(message, "warning")
                return

        node_attributes = []
        seen = set()
        for node in nodes:
            if node in seen or not cmds.objExists(node):
                continue
            seen.add(node)
            attributes = _corrective_value_attrs(node)
            if attributes:
                node_attributes.append((node, attributes))

        self.driver_tree.clear()
        self._driver_bindings = []
        for node, attributes in sorted(node_attributes, key=lambda value: _short(value[0]).lower()):
            top = QtWidgets.QTreeWidgetItem(
                ["{}    · {} 项".format(_short(node), len(attributes)), ""])
            top.setData(0, QtCore.Qt.ItemDataRole.UserRole, node)
            top.setToolTip(0, node)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            self.driver_tree.addTopLevelItem(top)
            rows = []
            for attribute in attributes:
                target_driver, move_axis = _connected_multiplier_info(
                    node, attribute)
                input_plug = _movement_branch_input(
                    node, attribute, target_driver, move_axis)
                rows.append((attribute, target_driver, move_axis, input_plug))

            target_rows = {}
            system_rows = []
            for row in rows:
                attribute, target_driver, move_axis, _input_plug = row
                if target_driver and move_axis:
                    target_rows.setdefault(target_driver, []).append(row)
                else:
                    system_rows.append(row)

            def add_value_item(parent, row):
                attribute, target_driver, move_axis, input_plug = row
                label = _friendly_driver_row(
                    attribute, target_driver, move_axis,
                    input_plug, node)
                item = QtWidgets.QTreeWidgetItem([label, ""])
                item.setData(0, QtCore.Qt.ItemDataRole.UserRole, node)
                item.setData(0, int(QtCore.Qt.ItemDataRole.UserRole) + 1, attribute)
                item.setData(
                    0, int(QtCore.Qt.ItemDataRole.UserRole) + 2,
                    target_driver or "")
                item.setData(
                    0, int(QtCore.Qt.ItemDataRole.UserRole) + 3,
                    move_axis or "")
                item.setData(
                    0, int(QtCore.Qt.ItemDataRole.UserRole) + 4,
                    input_plug or "")
                tooltip = "属性：{}.{}".format(node, attribute)
                if target_driver:
                    tooltip += "\n骨骼：{}".format(target_driver)
                if input_plug:
                    tooltip += "\n输入：{}".format(input_plug)
                if target_driver and move_axis:
                    tooltip += "\n输出：{}.translate{}".format(
                        target_driver, move_axis)
                if target_driver and move_axis:
                    tooltip += ("\n右键：选择输入通道或补充位移方向；"
                                "连续按两次右键：更换连接对象。")
                item.setToolTip(0, tooltip)
                parent.addChild(item)
                self.driver_tree.setItemWidget(
                    item, 1, self._driver_value_control(node, attribute, item))

            grouped = []
            for target_driver, group_rows in target_rows.items():
                grouped.append((
                    _short(target_driver), target_driver, group_rows))
            for _bone_name, target_driver, group_rows in sorted(
                    grouped, key=lambda value: value[0].lower()):
                for row in sorted(
                        group_rows,
                        key=lambda value: (value[2] or "", value[0])):
                    add_value_item(top, row)

            if system_rows:
                for row in sorted(
                        system_rows,
                        key=lambda value: _friendly_driver_attr(
                            value[0], value[1], value[2],
                            value[3], node).lower()):
                    add_value_item(top, row)

        if tree_state.get("had_items"):
            self.driver_tree.collapseAll()
        elif len(node_attributes) <= 2:
            self.driver_tree.expandAll()
        else:
            self.driver_tree.collapseAll()
        self._filter_driver_items()
        self._restore_driver_tree_state(tree_state)
        if node_attributes:
            message = "已读取 {} 个关节、{} 个驱动数值。".format(
                len(node_attributes), len(self._driver_bindings))
            self._show_status(message, "success")
        else:
            message = "没有找到修型驱动数值。请选择源关节，或扫描场景。"
            self._show_status(message, "warning")

    def _reveal_driver_manager_target(self, source, driver):
        """Expands and focuses the freshly created driver after a rescan."""
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        for top_index in range(self.driver_tree.topLevelItemCount()):
            top = self.driver_tree.topLevelItem(top_index)
            top_source = top.data(0, role)
            if not (top_source and _same_node(top_source, source)):
                continue
            top.setExpanded(True)
            for row_index in range(top.childCount()):
                row = top.child(row_index)
                target = row.data(0, role + 2)
                if target and _same_node(target, driver):
                    if top.isHidden() or row.isHidden():
                        return False
                    self.driver_tree.setCurrentItem(row)
                    self.driver_tree.scrollToItem(row)
                    return True
            return False
        return False

    def _filter_driver_items(self, *_):
        if not hasattr(self, "driver_tree"):
            return
        keyword = self.driver_search.text().strip().lower()
        adjusted_only = self.driver_filter.currentIndex() == 1
        visible_items = 0
        visible_nodes = set()

        bindings_by_item = {binding["item"]: binding for binding in self._driver_bindings}
        for top_index in range(self.driver_tree.topLevelItemCount()):
            top = self.driver_tree.topLevelItem(top_index)
            node = top.data(0, QtCore.Qt.ItemDataRole.UserRole) or ""
            top_match = keyword in (top.text(0) + " " + node).lower()
            visible_in_top = 0
            for row_index in range(top.childCount()):
                row = top.child(row_index)
                binding = bindings_by_item.get(row)
                if not binding:
                    row.setHidden(True)
                    continue
                role = int(QtCore.Qt.ItemDataRole.UserRole)
                target = row.data(0, role + 2) or ""
                input_plug = row.data(0, role + 4) or ""
                searchable = " ".join((
                    row.text(0), binding["attribute"],
                    target, input_plug))
                text_match = top_match or keyword in searchable.lower()
                adjusted_match = (
                    not adjusted_only or
                    abs(binding["spin"].value() -
                        binding["default"]) > 1e-6)
                visible = text_match and adjusted_match
                row.setHidden(not visible)
                if visible:
                    visible_in_top += 1
                    visible_items += 1
                    visible_nodes.add(node)
            top.setHidden(visible_in_top == 0)
            if keyword and visible_in_top:
                top.setExpanded(True)
        self.driver_summary.setText(
            "显示 {} 个关节 · {} 个驱动项".format(len(visible_nodes), visible_items))

    def _refresh_driver_values(self):
        refreshed = 0
        for binding in self._driver_bindings:
            if not cmds.objExists(binding["plug"]):
                continue
            value = float(cmds.getAttr(binding["plug"]))
            self._update_driver_controls(binding, value)
            refreshed += 1
        self._filter_driver_items()
        self._show_status("已刷新 {} 个驱动数值。".format(refreshed), "success")

    def _update_driver_controls(self, binding, value):
        spin = binding["spin"]
        slider = binding["slider"]
        spin.blockSignals(True)
        slider.blockSignals(True)
        spin.setValue(value)
        slider.setValue(self._slider_position(
            value, binding["minimum"], binding["maximum"]))
        spin.blockSignals(False)
        slider.blockSignals(False)

    def _apply_driver_value(self, binding, value, update_controls=False):
        try:
            if not binding.get("neutral_checked"):
                item = binding["item"]
                role = int(QtCore.Qt.ItemDataRole.UserRole)
                _normalize_legacy_branch_neutral(
                    binding["node"], binding["attribute"],
                    item.data(0, role + 2) or None,
                    item.data(0, role + 3) or None)
                binding["neutral_checked"] = True
            value = max(binding["minimum"], min(binding["maximum"], float(value)))
            cmds.setAttr(binding["plug"], value)
            if update_controls:
                self._update_driver_controls(binding, value)
            if self.driver_filter.currentIndex() == 1:
                self._filter_driver_items()
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")

    def _driver_slider_changed(self, binding, position):
        value = self._slider_value(
            position, binding["minimum"], binding["maximum"])
        binding["spin"].blockSignals(True)
        binding["spin"].setValue(value)
        binding["spin"].blockSignals(False)
        self._apply_driver_value(binding, value)

    def _driver_spin_changed(self, binding, value):
        binding["slider"].blockSignals(True)
        binding["slider"].setValue(self._slider_position(
            value, binding["minimum"], binding["maximum"]))
        binding["slider"].blockSignals(False)
        self._apply_driver_value(binding, value)

    def _begin_slider_edit(self):
        if not self._slider_undo_open:
            cmds.undoInfo(openChunk=True, chunkName="调整修型驱动数值")
            self._slider_undo_open = True

    def _end_slider_edit(self):
        if self._slider_undo_open:
            cmds.undoInfo(closeChunk=True)
            self._slider_undo_open = False

    def _reset_visible_driver_values(self):
        visible = [binding for binding in self._driver_bindings
                   if not binding["item"].isHidden() and
                   not binding["item"].parent().isHidden()]
        if not visible:
            self._show_status("当前没有可重置的驱动项。", "warning")
            return
        answer = QtWidgets.QMessageBox.question(
            self, "重置驱动数值",
            "将 {} 个当前可见驱动项恢复为生成时的默认值？".format(len(visible)),
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        cmds.undoInfo(openChunk=True, chunkName="重置修型驱动数值")
        try:
            for binding in visible:
                self._apply_driver_value(binding, binding["default"], True)
        finally:
            cmds.undoInfo(closeChunk=True)
        self._filter_driver_items()
        self._show_status("已重置 {} 个驱动项。".format(len(visible)), "success")

    @staticmethod
    def _driver_item_connection_data(item):
        if item is None:
            return None
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        node = item.data(0, role)
        attribute = item.data(0, role + 1)
        target = item.data(0, role + 2)
        move_axis = item.data(0, role + 3)
        if not (node and attribute and target and move_axis):
            return None
        return node, attribute, target, move_axis

    def _schedule_driver_channel_menu(self, position):
        item = self.driver_tree.itemAt(position)
        if not self._driver_item_connection_data(item):
            return
        self.driver_tree.setCurrentItem(item)
        global_position = self.driver_tree.viewport().mapToGlobal(position)
        self._show_driver_channel_menu(item, global_position)

    def _show_driver_channel_menu(self, item, global_position):
        data = self._driver_item_connection_data(item)
        if not data:
            return
        node, attribute, target, move_axis = data
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        input_plug = item.data(0, role + 4) or ""
        if not input_plug or not cmds.objExists(input_plug):
            input_plug = _movement_branch_input(
                node, attribute, target, move_axis) or ""
        current_channel = (input_plug.rsplit(".", 1)[-1]
                           if "." in input_plug else "")
        source_node = (input_plug.rsplit(".", 1)[0]
                       if "." in input_plug else node)

        menu = QtWidgets.QMenu(self)
        source_action = menu.addAction(
            "连接对象：{}".format(_short(source_node)))
        source_action.setEnabled(False)
        menu.addSeparator()
        for group_label, channels in DRIVER_CHANNEL_GROUPS:
            menu.addSection(group_label)
            for channel in channels:
                action = menu.addAction(channel)
                action.setCheckable(True)
                action.setChecked(channel == current_channel)
                action.triggered.connect(
                    lambda checked=False, selected=channel:
                    self._change_driver_branch_input(
                        item, selected, source_node))
        menu.addSeparator()
        surface_driver = (_surface_driver_from_selection(target)
                          if cmds.objExists(target) else None)
        add_axes = menu.addAction("补充其他位移方向…")
        add_axes.setEnabled(bool(surface_driver))
        if surface_driver:
            add_axes.setToolTip(
                "为当前自动修型骨骼补充缺少的 X／Y／Z 位移方向。")
            add_axes.triggered.connect(
                lambda checked=False, selected=surface_driver:
                QtCore.QTimer.singleShot(
                    0, lambda driver=selected:
                    self._open_add_surface_axes_dialog(driver)))
        else:
            add_axes.setToolTip("该功能仅适用于自动创建的表面修型骨骼。")
        delete_axes = menu.addAction(
            "删除当前位移 {} 驱动".format(move_axis))
        removable_axes = (_surface_driver_secondary_move_axes(surface_driver)
                          if surface_driver else ())
        can_delete_current = move_axis in removable_axes
        delete_axes.setEnabled(can_delete_current)
        if can_delete_current:
            delete_axes.setToolTip(
                "直接删除当前这一行的附加位移驱动；可使用 Maya Undo 撤销。")
            delete_axes.triggered.connect(
                lambda checked=False, selected=surface_driver,
                selected_axis=move_axis:
                QtCore.QTimer.singleShot(
                    0, lambda driver=selected, axis=selected_axis:
                    self._remove_surface_axis_direct(driver, axis)))
        elif surface_driver and move_axis in _surface_driver_existing_move_axes(
                surface_driver):
            delete_axes.setToolTip(
                "当前行是主位移方向，不能单独删除；请使用顶部按钮删除整根骨骼。")
        elif surface_driver:
            delete_axes.setToolTip("当前行没有可删除的附加位移驱动。")
        else:
            delete_axes.setToolTip("该功能仅适用于自动创建的表面修型骨骼。")
        menu.addSeparator()
        change_object = menu.addAction("更换连接对象…")
        change_object.triggered.connect(
            lambda checked=False: self._open_driver_source_dialog(item))
        self._active_driver_context_menu = menu
        self._active_driver_context_item = item
        menu.installEventFilter(self)
        menu.exec(global_position)
        if self._active_driver_context_menu is menu:
            self._active_driver_context_menu = None
            self._active_driver_context_item = None
        menu.deleteLater()

    def eventFilter(self, watched, event):
        if (watched is self._active_driver_context_menu and
                event.type() == QtCore.QEvent.Type.MouseButtonPress and
                event.button() == QtCore.Qt.MouseButton.RightButton):
            item = self._active_driver_context_item
            self._active_driver_context_menu = None
            self._active_driver_context_item = None
            watched.close()
            if self._driver_item_connection_data(item):
                QtCore.QTimer.singleShot(
                    0, lambda selected=item:
                    self._open_driver_source_dialog(selected))
            return True
        return super(_UI, self).eventFilter(watched, event)

    def _resolve_driver_source_text(self, text):
        text = text.strip()
        if not text:
            raise RuntimeError("请输入连接对象。")
        matches = cmds.ls(text, long=True) or []
        if not matches:
            raise RuntimeError("连接对象不存在：{}".format(text))
        if len(matches) > 1:
            raise RuntimeError(
                "场景中存在多个同名对象，请输入完整路径：{}".format(text))
        if cmds.nodeType(matches[0]) not in ("joint", "transform"):
            raise RuntimeError("连接对象必须是 joint 或 transform。")
        return matches[0]

    def _open_driver_source_dialog(self, item):
        data = self._driver_item_connection_data(item)
        if not data:
            return
        node, attribute, target, move_axis = data
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        input_plug = item.data(0, role + 4) or ""
        if not input_plug or not cmds.objExists(input_plug):
            input_plug = _movement_branch_input(
                node, attribute, target, move_axis) or ""
        current_source = (input_plug.rsplit(".", 1)[0]
                          if "." in input_plug else node)
        current_channel = (input_plug.rsplit(".", 1)[-1]
                           if "." in input_plug else "rotateZ")

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("更换连接对象")
        dialog.setModal(True)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            "{} 位移当前读取 {}".format(move_axis, current_channel)))
        row = QtWidgets.QHBoxLayout()
        field = QtWidgets.QLineEdit(current_source)
        read_selection = QtWidgets.QPushButton("读取当前选择")
        row.addWidget(field, 1)
        row.addWidget(read_selection)
        layout.addLayout(row)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def load_selection(_checked=False):
            selected = cmds.ls(selection=True, long=True) or []
            if selected:
                field.setText(selected[0])
            else:
                self._show_status("请先在 Maya 中选择连接对象。", "warning")

        read_selection.clicked.connect(load_selection)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        try:
            source_node = self._resolve_driver_source_text(field.text())
            self._change_driver_branch_input(
                item, current_channel, source_node)
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")

    def _open_add_surface_axes_dialog(self, driver):
        """Adds missing output movement axes without leaving driver manager."""
        if not (driver and cmds.objExists(driver) and cmds.attributeQuery(
                "isQuaternionSurfaceDriver", node=driver, exists=True)):
            message = "当前项目不是自动创建的表面修型骨骼。"
            cmds.warning(message)
            self._show_status(message, "error")
            return

        existing_axes = set(_surface_driver_existing_move_axes(driver))
        missing_axes = [axis for axis in "XYZ" if axis not in existing_axes]
        if not missing_axes:
            message = "{} 已包含 X、Y、Z 三个位移方向。".format(
                _short(driver))
            self._show_status(message, "success")
            QtWidgets.QMessageBox.information(
                self, "补充位移方向", message)
            return

        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("补充其他位移方向")
        dialog.setModal(True)
        dialog.setMinimumWidth(360)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            "当前修型骨骼：{}".format(_short(driver))))
        rotation_axis = _recorded_rotation_axis(driver)
        if rotation_axis:
            layout.addWidget(QtWidgets.QLabel(
                "继续读取父级旋转 {}，补充以下位移输出：".format(
                    rotation_axis)))
        else:
            layout.addWidget(QtWidgets.QLabel(
                "补充以下位移输出（沿用当前旋转输入）："))

        axes_layout = QtWidgets.QVBoxLayout()
        axis_checks = {}
        for axis in missing_axes:
            axis_row = QtWidgets.QHBoxLayout()
            control = QtWidgets.QCheckBox("添加位移 {}".format(axis))
            axis_checks[axis] = control
            axis_row.addWidget(control)
            axis_row.addStretch(1)
            axes_layout.addLayout(axis_row)
        layout.addLayout(axes_layout)

        distance_row = QtWidgets.QHBoxLayout()
        distance_row.addWidget(QtWidgets.QLabel("每旋转 1° 基础位移"))
        distance = self._number(
            self.extend_distance.value(), -1000.0, 1000.0, 0.01)
        distance.setToolTip(
            "决定旋转角度与位移距离的换算；新轴的倍率仍从 0 开始。")
        distance_row.addWidget(distance, 1)
        layout.addLayout(distance_row)

        hint = QtWidgets.QLabel(
            "新增位移倍率默认为 0，添加后可直接在当前管理页调节。")
        hint.setObjectName("hintLabel")
        layout.addWidget(hint)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok |
            QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        add_button = buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Ok)
        add_button.setText("添加所选方向")
        add_button.setEnabled(bool(missing_axes))
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("关闭")
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def accept_axes():
            chosen = [axis for axis in missing_axes
                      if axis_checks[axis].isChecked()]
            if not chosen:
                QtWidgets.QMessageBox.warning(
                    dialog, "补充位移方向", "请至少勾选一个缺少的位移方向。")
                return
            if abs(distance.value()) < 1e-8:
                QtWidgets.QMessageBox.warning(
                    dialog, "补充位移方向", "每旋转 1° 的基础位移不能为 0。")
                return
            dialog.accept()

        buttons.accepted.connect(accept_axes)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        axes = [axis for axis in missing_axes
                if axis_checks[axis].isChecked()]
        self.extend_distance.setValue(distance.value())
        self._add_surface_axes(
            [driver], axes, distance.value(), focus_driver=driver)

    def _remove_surface_axis_direct(self, driver, axis):
        """Deletes the exact secondary branch represented by the clicked row."""
        try:
            cmds.undoInfo(openChunk=True, chunkName="删除修型骨骼附加位移轴")
            try:
                renamed_driver, source, _attribute = remove_surface_move_axis(
                    driver, axis)
                if cmds.objExists(renamed_driver):
                    # Keep the post-delete selection inside the same undo
                    # chunk, so one Ctrl+Z restores both the branch and the
                    # artist's previous selection state.
                    cmds.select(renamed_driver, replace=True)
            finally:
                cmds.undoInfo(closeChunk=True)
        except RuntimeError as error:
            message = str(error)
            cmds.warning(message)
            self._show_status(message, "error")
            return

        if self.driver_filter.currentIndex() != 0:
            self.driver_filter.setCurrentIndex(0)
        self._last_axis_delete_source = source
        manager_sources = self._driver_manager_sources()
        if (cmds.objExists(source) and
                not any(_same_node(source, item) for item in manager_sources)):
            manager_sources.append(source)
        self._load_driver_manager(nodes_override=manager_sources)
        self._reveal_driver_manager_target(source, renamed_driver)
        self._show_status(
            "已删除 {} 的位移 {} 驱动；其他链接保持不变。".format(
                _short(renamed_driver), axis),
            "success")

    def _update_driver_items_after_rewire(
            self, old_target, new_target, changed_item, changed_input):
        """Updates affected rows in place without rebuilding/collapsing the tree."""
        role = int(QtCore.Qt.ItemDataRole.UserRole)
        for binding in self._driver_bindings:
            child = binding["item"]
            target = child.data(0, role + 2) or ""
            same_target = (
                target == old_target or
                (target and old_target and
                 _short(target) == _short(old_target)))
            if not same_target:
                continue
            node = child.data(0, role)
            attribute = child.data(0, role + 1)
            move_axis = child.data(0, role + 3) or ""
            input_plug = (changed_input if child is changed_item else
                          (child.data(0, role + 4) or ""))
            child.setData(0, role + 2, new_target)
            child.setData(0, role + 4, input_plug)
            child.setText(0, _friendly_driver_row(
                attribute, new_target, move_axis, input_plug, node))
            tooltip = "属性：{}.{}".format(node, attribute)
            tooltip += "\n骨骼：{}".format(new_target)
            if input_plug:
                tooltip += "\n输入：{}".format(input_plug)
            if move_axis:
                tooltip += "\n输出：{}.translate{}".format(
                    new_target, move_axis)
            tooltip += ("\n右键：选择输入通道或补充位移方向；"
                        "连续按两次右键：更换连接对象。")
            child.setToolTip(0, tooltip)
        self.driver_tree.setCurrentItem(changed_item)

    def _change_driver_branch_input(self, item, channel, source_node):
        data = self._driver_item_connection_data(item)
        if not data:
            return
        node, attribute, target, move_axis = data
        old_target = target
        try:
            cmds.undoInfo(openChunk=True, chunkName="修改修型驱动通道")
            try:
                source_plug = rewire_movement_branch(
                    node, attribute, target, move_axis,
                    source_node, channel)
                label = _surface_label_from_driver(
                    target, node, attribute)
                if label:
                    move_axes = list(_recorded_move_axes(target))
                    if not move_axes:
                        move_axes = [label[-1]]
                    if move_axis not in move_axes:
                        move_axes.append(move_axis)
                    _record_axis_metadata(
                        target, _recorded_rotation_axis(target),
                        label[-1], move_axes)
                target = repair_corrective_bone_names(
                    target, node, label)
            finally:
                cmds.undoInfo(closeChunk=True)
            if cmds.objExists(target):
                cmds.select(target, replace=True)
            self._update_driver_items_after_rewire(
                old_target, target, item, source_plug)
            self._show_status(
                "已连接：{} → {} 的 {} 位移。".format(
                    source_plug, _short(target), move_axis),
                "success")
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")

    def _select_driver_tree_item(self, item, _column):
        source_node = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        target_node = item.data(
            0, int(QtCore.Qt.ItemDataRole.UserRole) + 2)
        node = target_node or source_node
        if (self._display_mode == "skin" and target_node and
                cmds.objExists(target_node) and
                cmds.attributeQuery(
                    "isQuaternionSurfaceDriver",
                    node=target_node, exists=True)):
            node = self._skin_joint_for_driver(target_node) or target_node
        if node and cmds.objExists(node):
            cmds.select(node, replace=True)
            self._show_status("已在场景中选择：{}".format(_short(node)), "info")

    @staticmethod
    def _skin_joint_for_driver(driver):
        joints = _skin_joints_for_driver(driver)
        return joints[0] if joints else None

    def _install_maya_callbacks(self):
        """Listens only for history events; no polling or scene scanning."""
        for event_name in ("Undo", "Redo"):
            try:
                callback_id = om.MEventMessage.addEventCallback(
                    event_name, self._on_maya_history_event)
                self._maya_callback_ids.append(callback_id)
            except RuntimeError:
                pass

    def _remove_maya_callbacks(self):
        for callback_id in self._maya_callback_ids:
            try:
                om.MMessage.removeCallback(callback_id)
            except RuntimeError:
                pass
        self._maya_callback_ids = []

    def _on_maya_history_event(self, *_):
        """Schedules a tiny targeted refresh only for this tool's axis delete."""
        try:
            history_names = (
                cmds.undoInfo(query=True, undoName=True) or "",
                cmds.undoInfo(query=True, redoName=True) or "")
        except RuntimeError:
            return
        chunk_name = "删除修型骨骼附加位移轴"
        if not any(chunk_name in name for name in history_names):
            return
        if (self._manager_history_refresh_pending or
                self.driver_tree.topLevelItemCount() == 0):
            return
        self._manager_history_refresh_pending = True
        QtCore.QTimer.singleShot(0, self._refresh_manager_after_history)

    def _refresh_manager_after_history(self):
        self._manager_history_refresh_pending = False
        if not self.isVisible():
            return
        sources = self._driver_manager_sources()
        source = self._last_axis_delete_source
        if (source and cmds.objExists(source) and
                not any(_same_node(source, item) for item in sources)):
            sources.append(source)
        if sources:
            # Re-read only the already displayed sources.  This avoids the
            # full-scene joint/transform scan used by the manual scan button.
            self._load_driver_manager(nodes_override=sources)

    def closeEvent(self, event):
        self._remove_maya_callbacks()
        if self._slider_undo_open:
            try:
                cmds.undoInfo(closeChunk=True)
            except RuntimeError:
                pass
            self._slider_undo_open = False
        super(_UI, self).closeEvent(event)

    def _apply_style(self):
        self.setStyleSheet("""
            QDialog {
                background: #292929;
            }
            QLabel#titleLabel {
                font-size: 20px;
                font-weight: 600;
                color: #f0f0f0;
            }
            QLabel#hintLabel {
                color: #a9a9a9;
                padding: 1px 2px;
            }
            QLabel#fieldHelpLabel {
                color: #8f8f8f;
                font-size: 11px;
                padding: 0 2px 4px 2px;
            }
            QLabel#instructionLabel {
                color: #dddddd;
                background: #2b2b2b;
                border-left: 3px solid #4f9ac9;
                border-radius: 3px;
                padding: 9px 11px;
            }
            QLabel#exampleLabel {
                color: #c8dceb;
                background: #293741;
                border: 1px solid #3d596b;
                border-radius: 4px;
                padding: 8px 10px;
            }
            QFrame#boneDisplaySwitch {
                background: #333333;
                border: 1px solid #4b4b4b;
                border-radius: 5px;
            }
            QLabel#switchLabel {
                color: #a9a9a9;
                font-size: 11px;
            }
            QLabel#stepLabel {
                color: #d7d7d7;
                background: #343434;
                border-radius: 3px;
                padding: 9px;
            }
            QTabWidget::pane {
                border: 1px solid #444444;
                border-radius: 4px;
                background: #303030;
            }
            QTabBar::tab {
                background: #333333;
                color: #bdbdbd;
                min-width: 120px;
                padding: 9px 18px;
                border: 1px solid #444444;
            }
            QTabBar::tab:selected {
                background: #3d3d3d;
                color: #ffffff;
                border-bottom: 2px solid #4f9ac9;
            }
            QGroupBox {
                font-weight: 600;
                border: 1px solid #484848;
                border-radius: 5px;
                margin-top: 10px;
                background: #343434;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 5px;
                color: #e4e4e4;
            }
            QLineEdit, QComboBox, QDoubleSpinBox {
                min-height: 27px;
                padding: 0 6px;
                background: #242424;
                border: 1px solid #505050;
                border-radius: 3px;
                selection-background-color: #397ca8;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {
                border: 1px solid #579bc5;
            }
            QPushButton {
                min-height: 29px;
                padding: 2px 10px;
                background: #454545;
                border: 1px solid #585858;
                border-radius: 4px;
            }
            QPushButton:hover {
                background: #505050;
            }
            QPushButton:pressed {
                background: #383838;
            }
            QPushButton#primaryButton {
                color: white;
                font-weight: 600;
                background: #397ca8;
                border: 1px solid #4f94bd;
            }
            QPushButton#primaryButton:hover {
                background: #438ab6;
            }
            QPushButton#dangerButton {
                color: #f3d7d7;
                background: #603838;
                border: 1px solid #8b5151;
            }
            QPushButton#dangerButton:hover {
                color: white;
                background: #784242;
            }
            QPushButton#displayModeButton {
                min-width: 58px;
                min-height: 24px;
                padding: 0 8px;
                color: #bdbdbd;
                background: #292929;
                border: 1px solid #505050;
                border-radius: 3px;
            }
            QPushButton#displayModeButton:checked {
                color: white;
                background: #397ca8;
                border-color: #59a0c9;
            }
            QToolButton#collapseButton {
                color: #cfcfcf;
                border: none;
                padding: 5px 2px;
                text-align: left;
            }
            QFrame#collapsibleFrame {
                border-top: 1px solid #474747;
            }
            QFrame#statusFrame {
                background: #333333;
                border: 1px solid #454545;
                border-radius: 4px;
            }
            QTreeWidget#driverTree {
                background: #292929;
                alternate-background-color: #303030;
                border: 1px solid #494949;
                border-radius: 4px;
                outline: none;
            }
            QTreeWidget#driverTree::item {
                min-height: 31px;
                border-bottom: 1px solid #383838;
            }
            QTreeWidget#driverTree::item:selected {
                background: #365d76;
            }
            QHeaderView::section {
                background: #3b3b3b;
                color: #d5d5d5;
                padding: 7px;
                border: none;
                border-right: 1px solid #505050;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #1f1f1f;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #4b91ba;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #d0d0d0;
                border: 1px solid #5a5a5a;
                width: 12px;
                margin: -5px 0;
                border-radius: 6px;
            }
            QScrollArea, QWidget#pageContent {
                background: #303030;
                border: none;
            }
            QScrollBar:vertical {
                background: #2a2a2a;
                width: 10px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: #555555;
                min-height: 28px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical:hover {
                background: #686868;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

    @staticmethod
    def _marked_nodes(attribute):
        return cmds.ls("*.{}".format(attribute), objectsOnly=True, long=True) or []

    def _initialize_default_display_mode(self):
        """Applies driver mode on open without warning in an empty scene."""
        try:
            has_correctives = bool(
                self._marked_nodes("isQuaternionCorrectiveRoot") or
                self._marked_nodes("isQuaternionSurfaceDriver") or
                self._marked_nodes("isQuaternionCorrectiveSkinJoint"))
        except (AttributeError, RuntimeError):
            has_correctives = False
        if has_correctives:
            self._set_bone_display_mode("driver")

    @staticmethod
    def _ensure_display_layer(name):
        if cmds.objExists(name):
            if cmds.nodeType(name) != "displayLayer":
                raise RuntimeError("{} 已存在，但不是显示层。".format(name))
            return name
        return cmds.createDisplayLayer(name=name, empty=True)

    def _sync_bone_display_layers(self):
        """Updates display-layer membership; expensive normalization runs once."""
        driver_nodes = sorted(set(
            self._marked_nodes("isQuaternionCorrectiveRoot") +
            self._marked_nodes("isQuaternionSurfaceDriver")))
        skin_nodes = sorted(set(
            self._marked_nodes("isQuaternionCorrectiveSkinJoint")))
        if not driver_nodes and not skin_nodes:
            return None, None, 0, 0

        driver_layer = self._ensure_display_layer(DRIVER_DISPLAY_LAYER)
        skin_layer = self._ensure_display_layer(SKIN_DISPLAY_LAYER)

        def add_missing(layer, nodes):
            members = cmds.editDisplayLayerMembers(
                layer, query=True, fullNames=True) or []
            existing = set(cmds.ls(members, long=True) or [])
            missing = [node for node in nodes if node not in existing]
            if missing:
                cmds.editDisplayLayerMembers(layer, missing, noRecurse=True)
            return missing

        new_driver_nodes = add_missing(driver_layer, driver_nodes)
        new_skin_nodes = add_missing(skin_layer, skin_nodes)

        # Migrate visibility left by the previous per-joint switch.  New nodes
        # are also normalized once; afterwards only two layer attributes change.
        normalize = ((driver_nodes + skin_nodes) if not self._display_layers_initialized
                     else (new_driver_nodes + new_skin_nodes))
        for node in normalize:
            plug = node + ".visibility"
            if cmds.objExists(plug) and cmds.getAttr(plug, settable=True):
                cmds.setAttr(plug, True)
        self._display_layers_initialized = True
        self._driver_display_count = len(driver_nodes)
        self._skin_display_count = len(skin_nodes)
        return driver_layer, skin_layer, len(driver_nodes), len(skin_nodes)

    def _apply_display_layer_visibility(self, mode, driver_layer, skin_layer):
        show_drivers = mode == "driver"
        cmds.setAttr(driver_layer + ".visibility", show_drivers)
        cmds.setAttr(skin_layer + ".visibility", not show_drivers)
        driver_group = (self.driver_group_field.text().strip()
                        if hasattr(self, "driver_group_field") else "")
        if (driver_group and cmds.objExists(driver_group) and
                cmds.getAttr(driver_group + ".visibility", settable=True)):
            cmds.setAttr(driver_group + ".visibility", True)

    def _set_bone_display_mode(self, mode):
        """Switches two display layers instead of editing every joint."""
        errors = []
        driver_count = skin_count = 0
        cmds.undoInfo(openChunk=True, chunkName="切换修型骨骼显示")
        cmds.refresh(suspend=True)
        try:
            layers_ready = (
                self._display_layers_initialized and
                cmds.objExists(DRIVER_DISPLAY_LAYER) and
                cmds.objExists(SKIN_DISPLAY_LAYER))
            if layers_ready:
                driver_layer = DRIVER_DISPLAY_LAYER
                skin_layer = SKIN_DISPLAY_LAYER
                driver_count = self._driver_display_count
                skin_count = self._skin_display_count
            else:
                driver_layer, skin_layer, driver_count, skin_count = (
                    self._sync_bone_display_layers())
            if not driver_layer or not skin_layer:
                raise RuntimeError("场景中没有找到本工具生成的驱动骨骼或蒙皮修型骨骼。")
            self._apply_display_layer_visibility(mode, driver_layer, skin_layer)
            self._display_mode = mode
        except RuntimeError as error:
            errors.append(str(error))
        finally:
            cmds.refresh(suspend=False)
            cmds.refresh(force=True)
            cmds.undoInfo(closeChunk=True)

        if errors:
            message = " | ".join(errors)
            cmds.warning(message)
            self._show_status(message, "warning")
            return
        if mode == "driver":
            message = "当前显示 {} 个驱动骨骼；已隐藏 {} 个蒙皮修型骨骼。".format(
                driver_count, skin_count)
        else:
            message = "当前显示 {} 个蒙皮修型骨骼；已隐藏 {} 个驱动骨骼。".format(
                skin_count, driver_count)
        self._show_status(message, "success")

    def _show_status(self, message, level="info"):
        colors = {
            "info": ("#333333", "#555555"),
            "success": ("#263b31", "#3f7656"),
            "warning": ("#443824", "#8a6b32"),
            "error": ("#482c2c", "#8c4b4b"),
        }
        background, border = colors.get(level, colors["info"])
        self.status_frame.setStyleSheet(
            "QFrame#statusFrame {background: %s; border: 1px solid %s; border-radius: 4px;}" %
            (background, border))
        self.status_label.setText(message)

    def _load_selected(self, field, expected_type):
        selected = cmds.ls(selection=True, long=True) or []
        if len(selected) != 1:
            message = "请先选择一个{}。".format(
                "网格" if expected_type == "mesh" else "变换节点")
            cmds.warning(message)
            self._show_status(message, "warning")
            return
        node = selected[0]
        if expected_type == "mesh":
            try:
                _mesh_shape(node)
            except RuntimeError as error:
                cmds.warning(str(error))
                self._show_status(str(error), "error")
                return
        field.setText(node)
        self._show_status("已读取：{}".format(_short(node)), "success")

    @staticmethod
    def _bend_direction(combo):
        value = combo.currentData()
        if value is None:
            value = combo.currentText()
        return "自动" if value.startswith("自动") else value

    def _settings(self):
        surface_axes = [axis for axis, control in (
            ("X", self.create_axis_x),
            ("Y", self.create_axis_y),
            ("Z", self.create_axis_z)) if control.isChecked()]
        if not surface_axes:
            raise RuntimeError("请至少勾选一个关节创建方向。")
        return {
            "rotation_axis": self.rotation_axis.currentText(),
            "bend_direction": self._bend_direction(self.rotation_sign),
            "surface_axes": surface_axes,
            "surface_axis": surface_axes[0],
            "probe_distance": self.probe_distance.value(),
            "base_distance": self.base_distance.value(),
            "extra_axes": [],
            "second_axis": "无",
            "second_base_distance": 0.02,
            "manual_second_axis": "无",
            "manual_second_distance": 0.02,
            "weight": self.weight.value(),
            "inverse": self.inverse_quaternion.isChecked(),
        }

    def _manual_settings(self):
        extra_axes = [axis for axis, control in (
            ("X", self.manual_extra_axis_x),
            ("Y", self.manual_extra_axis_y),
            ("Z", self.manual_extra_axis_z)) if control.isChecked()]
        extra_distance = self.manual_extra_distance.value()
        return {
            "rotation_axis": self.manual_rotation_axis.currentData(),
            "surface_axis": self.manual_surface_axis.currentData(),
            "base_distance": self.manual_base_distance.value(),
            "signed_rotation": True,
            "extra_axes": extra_axes,
            "second_axis": "无",
            "second_base_distance": extra_distance,
            "manual_second_axis": "无",
            "manual_second_distance": extra_distance,
            "keep_original_parent_space": (
                self.manual_keep_original_parent.isChecked()),
        }

    def create(self, *_):
        selected = cmds.ls(selection=True, type="joint", long=True) or []
        if not selected:
            message = "请选择一个或多个源关节。"
            cmds.warning(message)
            self._show_status(message, "warning")
            return
        mesh = self.mesh_field.text().strip()
        driver = self.driver_group_field.text().strip()
        if not mesh or not cmds.objExists(mesh):
            message = "请读取一个有效的表面网格。"
            cmds.warning(message)
            self._show_status(message, "error")
            return
        try:
            _mesh_shape(mesh)
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")
            return
        if not driver:
            message = "驱动骨骼组名称不能为空。"
            cmds.warning(message)
            self._show_status(message, "error")
            return
        if cmds.objExists(driver) and cmds.nodeType(driver) != "transform":
            message = "驱动骨骼组必须是 transform 节点。"
            cmds.warning(message)
            self._show_status(message, "error")
            return
        try:
            settings = self._settings()
        except RuntimeError as error:
            message = str(error)
            cmds.warning(message)
            self._show_status(message, "error")
            return
        try:
            cmds.loadPlugin("quatNodes", quiet=True)
        except RuntimeError as error:
            message = "无法加载 quatNodes 插件：{}".format(error)
            cmds.warning(message)
            self._show_status(message, "error")
            return
        created, errors = [], []
        cmds.undoInfo(openChunk=True, chunkName="创建四元数修型骨骼")
        try:
            if not cmds.objExists(driver):
                driver = cmds.group(empty=True, name=driver)
                self.driver_group_field.setText(driver)
            for joint in selected:
                try:
                    created.extend(create_for_joint(
                        joint, mesh, driver, settings))
                except RuntimeError as error:
                    errors.append("{}: {}".format(_short(joint), error))
            if created:
                cmds.select(created, replace=True)
                if self._display_mode:
                    cmds.refresh(suspend=True)
                    try:
                        driver_layer, skin_layer, _driver_count, _skin_count = (
                            self._sync_bone_display_layers())
                        if driver_layer and skin_layer:
                            self._apply_display_layer_visibility(
                                self._display_mode, driver_layer, skin_layer)
                    finally:
                        cmds.refresh(suspend=False)
                        cmds.refresh(force=True)
        finally:
            cmds.undoInfo(closeChunk=True)
        message = "已为 {} 个关节创建 {} 个节点。".format(len(selected), len(created))
        if errors:
            message += " 已跳过：" + " | ".join(errors)
            cmds.warning(message)
        self._show_status(message, "warning" if errors else "success")

    def _selected_surface_drivers(self):
        selected = cmds.ls(selection=True, long=True) or []
        resolved = []
        ignored = []
        for node in selected:
            driver = _surface_driver_from_selection(node)
            if driver and driver not in resolved:
                resolved.append(driver)
            elif not driver:
                ignored.append(_short(node))
        if not resolved:
            raise RuntimeError("请选择 + / - 表面修型驱动骨骼，或其对应的蒙皮修型骨骼。")
        return resolved, ignored

    def _selected_delete_targets(self):
        """Accepts individual surface joints as well as complete parent roots."""
        selected = cmds.ls(selection=True, long=True) or []
        roots = []
        drivers = []
        ignored = []
        for node in selected:
            root = _corrective_root_from_selection(node)
            if root:
                if root not in roots:
                    roots.append(root)
                continue
            driver = _surface_driver_from_selection(node)
            if driver:
                if driver not in drivers:
                    drivers.append(driver)
            else:
                ignored.append(_short(node))
        covered = set()
        for root in roots:
            covered.update(_surface_drivers_under_root(root))
        drivers = [driver for driver in drivers if driver not in covered]
        if not roots and not drivers:
            raise RuntimeError(
                "请选择修型父级骨骼、+ / - 表面驱动骨骼，"
                "或对应的蒙皮修型骨骼。")
        return roots, drivers, ignored

    def _add_surface_axes(self, drivers, axes, base_distance,
                          ignored=(), focus_driver=None):
        """Shared worker for page-based and context-menu axis additions."""
        created = []
        updated_drivers = []
        errors = []
        focus_source = None
        focus_result = None
        cmds.undoInfo(openChunk=True, chunkName="补加修型骨骼位移轴")
        try:
            for driver in drivers:
                focus_this_driver = bool(
                    focus_driver and _same_node(driver, focus_driver))
                naming_source = None
                naming_label = None
                for axis in axes:
                    try:
                        source, attribute, _nodes = add_surface_move_axis(
                            driver, axis, base_distance)
                        created.append("{}.{}".format(_short(source), attribute.rsplit(".", 1)[-1]))
                        naming_source = source
                        naming_label = _surface_label_from_driver(
                            driver, source, attribute.rsplit(".", 1)[-1])
                    except RuntimeError as error:
                        errors.append(str(error))
                if naming_source:
                    driver = repair_corrective_bone_names(
                        driver, naming_source, naming_label)
                    if focus_this_driver:
                        focus_source = naming_source
                        focus_result = driver
                updated_drivers.append(driver)
        finally:
            cmds.undoInfo(closeChunk=True)

        if updated_drivers:
            cmds.select(updated_drivers, replace=True)
        message = "已补加 {} 个位移轴链接。".format(len(created))
        if ignored:
            message += " 已忽略：{}。".format("、".join(ignored))
        if created:
            if focus_driver and self.driver_filter.currentIndex() != 0:
                self.driver_filter.setCurrentIndex(0)
            self._load_driver_manager(True)
            if focus_source and focus_result:
                self._reveal_driver_manager_target(
                    focus_source, focus_result)
        if errors:
            message += " " + " | ".join(errors)
            cmds.warning(message)
        elif created:
            message += " “驱动管理”页已刷新，可直接调节新倍率。"
        self._show_status(message, "warning" if errors else "success")

    def add_axes_to_selected(self, *_):
        axes = [axis for axis, control in (
            ("X", self.extend_axis_x),
            ("Y", self.extend_axis_y),
            ("Z", self.extend_axis_z)) if control.isChecked()]
        if not axes:
            message = "请至少勾选一个需要补加的位移轴。"
            cmds.warning(message)
            self._show_status(message, "warning")
            return
        try:
            drivers, ignored = self._selected_surface_drivers()
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")
            return
        self._add_surface_axes(
            drivers, axes, self.extend_distance.value(), ignored=ignored)

    def delete_selected_correctives(self, *_):
        try:
            roots, drivers, ignored = self._selected_delete_targets()
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")
            return
        surface_count = len(drivers) + sum(
            len(_surface_drivers_under_root(root)) for root in roots)
        names = "、".join(
            [_short(root) for root in roots] +
            [_short(driver) for driver in drivers])
        target_summary = []
        if roots:
            target_summary.append("{} 个父级系统".format(len(roots)))
        if surface_count:
            target_summary.append("{} 个表面修型骨骼".format(surface_count))
        answer = QtWidgets.QMessageBox.question(
            self, "删除修型骨骼",
            "删除{}及其蒙皮骨骼和驱动链接？\n{}".format(
                "、".join(target_summary), names),
            QtWidgets.QMessageBox.StandardButton.Yes |
            QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        deleted = []
        deleted_roots = []
        skin_count = graph_count = attr_count = 0
        errors = []
        cmds.undoInfo(openChunk=True, chunkName="删除修型骨骼及链接")
        try:
            for driver in drivers:
                try:
                    skins, graph_nodes, attributes = delete_surface_corrective(
                        driver)
                    deleted.append(driver)
                    skin_count += len(skins)
                    graph_count += len(graph_nodes)
                    attr_count += len(attributes)
                except RuntimeError as error:
                    errors.append(str(error))
            for root in roots:
                try:
                    (root_drivers, skins,
                     graph_nodes, attributes) = delete_corrective_root(root)
                    deleted.extend(root_drivers)
                    deleted_roots.append(root)
                    skin_count += len(skins)
                    graph_count += len(graph_nodes)
                    attr_count += len(attributes)
                except RuntimeError as error:
                    errors.append(str(error))
        finally:
            cmds.undoInfo(closeChunk=True)
        cmds.select(clear=True)
        if deleted or deleted_roots:
            self._load_driver_manager(True)
            self._display_layers_initialized = False
        message = (
            "已删除 {} 个父级系统、{} 个表面驱动骨骼、{} 个蒙皮骨骼、"
            "{} 个链接节点和 {} 个属性。"
            .format(len(deleted_roots), len(deleted), skin_count,
                    graph_count, attr_count))
        if ignored:
            message += " 已忽略：{}。".format("、".join(ignored))
        if errors:
            message += " " + " | ".join(errors)
            cmds.warning(message)
        self._show_status(message, "warning" if errors else "success")

    def disconnect_selected(self, *_):
        try:
            drivers, ignored = self._selected_surface_drivers()
            errors = []
            for driver in drivers:
                try:
                    disconnect_surface_translate(driver)
                except RuntimeError as error:
                    errors.append(str(error))
            cmds.select(drivers, replace=True)
            message = "已断开 {} 个骨骼的位移链接；现在可调整位置。".format(len(drivers))
            if ignored:
                message += " 已忽略：{}。".format("、".join(ignored))
            if errors:
                message += " " + " | ".join(errors)
            self._show_status(message, "warning" if errors else "success")
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")

    def reconnect_selected(self, *_):
        try:
            drivers, ignored = self._selected_surface_drivers()
            errors = []
            for driver in drivers:
                try:
                    reconnect_surface_translate(driver, self.update_direction.isChecked())
                except RuntimeError as error:
                    errors.append(str(error))
            cmds.select(drivers, replace=True)
            message = "已为 {} 个骨骼保存位置并重新连接。".format(len(drivers))
            if ignored:
                message += " 已忽略：{}。".format("、".join(ignored))
            if errors:
                message += " " + " | ".join(errors)
            self._show_status(message, "warning" if errors else "success")
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")

    def create_manual_link(self, *_):
        selected = (cmds.ls(orderedSelection=True, long=True) or
                    cmds.ls(selection=True, long=True) or [])
        if len(selected) != 2:
            message = "请先选择重新摆放的骨骼，再加选负责旋转的源关节（共两个关节）。"
            cmds.warning(message)
            self._show_status(message, "warning")
            return
        template, source = selected
        undo_open = False
        try:
            settings = self._manual_settings()
            cmds.undoInfo(openChunk=True, chunkName="建立新增修型驱动")
            undo_open = True
            driver, skin, nodes = create_manual_corrective_pair(
                template, source, settings)
            cmds.select([driver, source], replace=True)
            if self._display_mode:
                driver_layer, skin_layer, _driver_count, _skin_count = (
                    self._sync_bone_display_layers())
                if driver_layer and skin_layer:
                    self._apply_display_layer_visibility(
                        self._display_mode, driver_layer, skin_layer)
            self._load_driver_manager(True)
            found_in_manager = self._reveal_driver_manager_target(
                source, driver)
            axes = [settings["surface_axis"]]
            axes.extend(_additional_move_axes(
                settings, settings["surface_axis"]))
            driver_parents = cmds.listRelatives(
                driver, parent=True, fullPath=True) or []
            follow_nodes = []
            if (driver_parents and cmds.attributeQuery(
                    "qcFollowNode", node=driver_parents[0], exists=True)):
                follow_nodes = cmds.listConnections(
                    driver_parents[0] + ".qcFollowNode",
                    source=True, destination=False) or []
            follow_label = (_short(follow_nodes[0])
                            if follow_nodes else _short(source))
            message = (
                "已建立或更新驱动骨骼 {}，配套蒙皮骨骼 {}；"
                "旋转源：{}，空间跟随：{}，移动轴：{}。{}".format(
                    _short(driver), _short(skin), _short(source),
                    follow_label,
                    "、".join("自身 +{}".format(axis) for axis in axes),
                    ("驱动管理已刷新并定位到新增项目。"
                     if found_in_manager else
                     "驱动管理已刷新；当前筛选条件可能隐藏了新增项目。")))
            self._show_status(message, "success")
        except RuntimeError as error:
            cmds.warning(str(error))
            self._show_status(str(error), "error")
        finally:
            if undo_open:
                cmds.undoInfo(closeChunk=True)


_INSTANCE = None


def show():
    global _INSTANCE
    # Remove a window left open by the earlier maya.cmds UI implementation.
    if cmds.window(WINDOW, exists=True):
        cmds.deleteUI(WINDOW)
    try:
        if _INSTANCE is not None:
            _INSTANCE.close()
            _INSTANCE.deleteLater()
    except RuntimeError:
        pass
    for widget in QtWidgets.QApplication.topLevelWidgets():
        if widget.objectName() == WINDOW:
            widget.close()
            widget.deleteLater()
    _INSTANCE = _UI()
    _INSTANCE.show()
    _INSTANCE.raise_()
    _INSTANCE.activateWindow()
    return _INSTANCE
