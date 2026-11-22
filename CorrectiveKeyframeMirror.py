# -*- coding: utf-8 -*-
"""Maya 修型骨骼关键帧镜像工具。

用法（在 Maya 的 Python 选项卡运行）：
    import CorrectiveKeyframeMirror as qkm
    qkm.show()
"""
from __future__ import absolute_import

import re

import maya.cmds as cmds


WINDOW_NAME = "CorrectiveKeyframeMirrorWindow"
WINDOW_TITLE = u"修型骨骼关键帧镜像"
# Source-joint attributes that belong to the quaternion-corrective system.
# This includes the root's quaternion blend weight as well as all automatic and
# manual Pos/Neg surface-movement multiplier attributes.
CORRECTIVE_ATTR_RE = re.compile(
    r"^(?:quatCorrectiveWeight|quatCorrectiveRotationMoveMultiplier|qc.*Move(?:[XYZ])?Multiplier)$"
)


def _short_name(node):
    return node.rsplit("|", 1)[-1]


def _replace_side(value, source_side, target_side):
    """Only replace complete side tokens, preserving names such as ``Left``."""
    # The side token includes its leading underscore (``_L`` / ``_R``), so the
    # character before it is normally a letter in names such as ``Knee_L``.
    # Only the character *after* the token needs a boundary check.
    return re.sub(r"%s(?![A-Za-z0-9])" % re.escape(source_side), target_side, value)


def _swap_surface_direction(value):
    """Return the opposite corrective surface direction in a node/attribute name.

    A mirrored manual corrective frequently changes from ``NegY`` on the left to
    ``PosY`` on the right (and vice versa).  This is only used as a fallback
    after the same-direction right-side target was not found.
    """
    match = re.search(r"(Pos|Neg)([XYZ])", value)
    if not match:
        return value
    replacement = ("Neg" if match.group(1) == "Pos" else "Pos") + match.group(2)
    return value[:match.start()] + replacement + value[match.end():]


def _first_existing_plug(node, attr_candidates):
    for attr in attr_candidates:
        plug = node + "." + attr
        if cmds.objExists(plug):
            return plug
    return None


def _first_existing_joint(short_name_candidates):
    for short_name in short_name_candidates:
        node = _node_from_short_name(short_name, node_type="joint")
        if node:
            return node
    return None


def _side_pair_from_name(name, direction):
    """Return source/target side tokens for a node name and requested direction."""
    leaf = _short_name(name)
    if direction == "L_TO_R":
        if "_L" not in leaf:
            return None
        return "_L", "_R"
    if direction == "R_TO_L":
        if "_R" not in leaf:
            return None
        return "_R", "_L"
    raise ValueError("Unsupported mirror direction: %s" % direction)


def _node_from_short_name(short_name, node_type=None):
    matches = cmds.ls(short_name, long=True, type=node_type) if node_type else cmds.ls(short_name, long=True)
    if not matches:
        return None
    if len(matches) > 1:
        raise RuntimeError(u"场景中存在重名节点，无法确定目标：%s" % short_name)
    return matches[0]


def _safe_key_count(plug):
    try:
        return int(cmds.keyframe(plug, query=True, keyframeCount=True) or 0)
    except RuntimeError:
        return 0


def _is_anim_curve(node):
    return bool(node) and cmds.nodeType(node).startswith("animCurve")


def _mirror_driver_plug(source_plug, source_side, target_side):
    """Resolve the opposite-side input plug for a driven-key curve."""
    target_plug = _replace_side(source_plug, source_side, target_side)
    if not cmds.objExists(target_plug):
        raise RuntimeError(u"未找到 Driven Key 的目标驱动属性：%s" % target_plug)
    return target_plug


def _delete_target_anim_curve(target_plug):
    """Remove a previous direct animCurve connection before replacing it."""
    for source_plug in cmds.listConnections(target_plug, source=True, destination=False, plugs=True) or []:
        node = source_plug.rsplit(".", 1)[0]
        if _is_anim_curve(node):
            cmds.delete(node)


def _copy_driven_key_curve(source_plug, target_plug, source_side, target_side):
    """Rebuild an SDK curve using the mirrored driver.

    Corrective multipliers are normally driven by ``animCurveUU`` nodes.  Their
    keys use *float* input values (joint rotation), not timeline frames, so
    ``setKeyframe`` cannot reproduce them.  The source curve is duplicated
    intact on a newly-created driven-key curve for the opposite source joint.
    """
    source_connections = cmds.listConnections(source_plug, source=True, destination=False, plugs=True) or []
    if not source_connections:
        return 0
    source_curve = source_connections[0].rsplit(".", 1)[0]
    if not _is_anim_curve(source_curve):
        return 0

    float_keys = cmds.keyframe(source_curve, query=True, floatChange=True) or []
    if not float_keys:
        return 0

    curve_input = cmds.listConnections(source_curve + ".input", source=True, destination=False, plugs=True) or []
    if not curve_input:
        raise RuntimeError(u"Driven Key 缺少输入连接：%s" % source_curve)
    source_input = curve_input[0]
    source_input_node = source_input.rsplit(".", 1)[0]

    if cmds.nodeType(source_input_node) == "unitConversion" and source_input.endswith(".output"):
        upstream = cmds.listConnections(source_input_node + ".input", source=True, destination=False, plugs=True) or []
        if not upstream:
            raise RuntimeError(u"Driven Key 单位转换缺少输入：%s" % source_input_node)
        target_driver = _mirror_driver_plug(upstream[0], source_side, target_side)
    else:
        target_driver = _mirror_driver_plug(source_input, source_side, target_side)

    values = cmds.keyframe(source_curve, query=True, valueChange=True) or []
    in_types = cmds.keyTangent(source_curve, query=True, inTangentType=True) or []
    out_types = cmds.keyTangent(source_curve, query=True, outTangentType=True) or []
    old_driver_value = cmds.getAttr(target_driver)
    _delete_target_anim_curve(target_plug)
    try:
        for index, driver_value in enumerate(float_keys):
            cmds.setAttr(target_driver, driver_value)
            cmds.setAttr(target_plug, values[index])
            cmds.setDrivenKeyframe(target_plug, currentDriver=target_driver)
    finally:
        cmds.setAttr(target_driver, old_driver_value)

    target_connections = cmds.listConnections(target_plug, source=True, destination=False, plugs=True) or []
    target_curve = target_connections[0].rsplit(".", 1)[0] if target_connections else None
    if _is_anim_curve(target_curve):
        for index, driver_value in enumerate(float_keys):
            tangent_args = {}
            if index < len(in_types):
                tangent_args["inTangentType"] = in_types[index]
            if index < len(out_types):
                tangent_args["outTangentType"] = out_types[index]
            if tangent_args:
                cmds.keyTangent(target_curve, edit=True, float=(driver_value, driver_value),
                                **tangent_args)
    return len(float_keys)


def _copy_curve(source_plug, target_plug, negate=False):
    """Replace target animation with source keys and their tangent types.

    ``copyKey/pasteKey`` relies on Maya's UI clipboard and is unreliable from a
    batch session.  Rebuilding the curve from queried key data works the same
    in the Script Editor, a shelf button, and mayapy.
    """
    count = _safe_key_count(source_plug)
    if not count:
        return 0
    if not cmds.objExists(target_plug):
        return 0
    if cmds.getAttr(target_plug, lock=True):
        raise RuntimeError(u"目标属性已锁定：%s" % target_plug)

    times = cmds.keyframe(source_plug, query=True, timeChange=True) or []
    values = cmds.keyframe(source_plug, query=True, valueChange=True) or []
    in_types = cmds.keyTangent(source_plug, query=True, inTangentType=True) or []
    out_types = cmds.keyTangent(source_plug, query=True, outTangentType=True) or []
    in_angles = cmds.keyTangent(source_plug, query=True, inAngle=True) or []
    out_angles = cmds.keyTangent(source_plug, query=True, outAngle=True) or []

    cmds.cutKey(target_plug, clear=True)
    for index, time in enumerate(times):
        value = values[index]
        if negate:
            value *= -1.0
        cmds.setKeyframe(target_plug, time=time, value=value)
        tangent_args = {}
        if index < len(in_types):
            tangent_args["inTangentType"] = in_types[index]
        if index < len(out_types):
            tangent_args["outTangentType"] = out_types[index]
        if tangent_args:
            cmds.keyTangent(target_plug, edit=True, time=(time, time), **tangent_args)
        # Fixed/linear keys use explicit angles.  Maya ignores these for spline
        # keys, so applying them only when both values are available is safe.
        if index < len(in_angles) and index < len(out_angles):
            try:
                cmds.keyTangent(target_plug, edit=True, time=(time, time),
                                inAngle=in_angles[index], outAngle=out_angles[index])
            except RuntimeError:
                pass
    return count


def _find_corrective_joints(source_joint, source_side):
    """Find auto-created driver/skin joints named after the selected source joint.

    The name test intentionally requires ``_QC_``: ordinary rig joints sharing a
    source name are never included.
    """
    source_leaf = _short_name(source_joint)
    result = []
    for joint in cmds.ls(type="joint", long=True) or []:
        leaf = _short_name(joint)
        if source_leaf in leaf and "_QC_" in leaf and source_side in leaf:
            result.append(joint)
    return sorted(set(result))


def _mirror_multiplier_attributes(source_joint, source_side, target_side, report):
    source_leaf = _short_name(source_joint)
    target_leaf = _replace_side(source_leaf, source_side, target_side)
    target_joint = _node_from_short_name(target_leaf, node_type="joint")
    if not target_joint:
        report["missing"].append(u"未找到目标源关节：%s" % target_leaf)
        return

    attrs = cmds.listAttr(source_joint, userDefined=True, keyable=True) or []
    for attr in attrs:
        if not CORRECTIVE_ATTR_RE.match(attr):
            continue
        source_plug = source_joint + "." + attr
        if not _safe_key_count(source_plug):
            continue
        direct_attr = _replace_side(attr, source_side, target_side)
        swapped_attr = _swap_surface_direction(direct_attr)
        target_plug = _first_existing_plug(target_joint, (direct_attr, swapped_attr))
        if not target_plug:
            report["missing"].append(u"未找到目标位移属性：%s" % (target_joint + "." + direct_attr))
            continue
        incoming = cmds.listConnections(source_plug, source=True, destination=False, plugs=True) or []
        source_curve = incoming[0].rsplit(".", 1)[0] if incoming else None
        if _is_anim_curve(source_curve) and (cmds.keyframe(source_curve, query=True, floatChange=True) or []):
            report["keys"] += _copy_driven_key_curve(source_plug, target_plug,
                                                       source_side, target_side)
        else:
            report["keys"] += _copy_curve(source_plug, target_plug)
        report["attributes"].append((source_plug, target_plug))


def _mirror_corrective_joint_translates(source_joint, source_side, target_side,
                                        negate_axes, report):
    for source_corrective in _find_corrective_joints(source_joint, source_side):
        source_leaf = _short_name(source_corrective)
        direct_leaf = _replace_side(source_leaf, source_side, target_side)
        swapped_leaf = _swap_surface_direction(direct_leaf)
        target_corrective = _first_existing_joint((direct_leaf, swapped_leaf))
        if not target_corrective:
            report["missing"].append(u"未找到目标修型骨骼：%s" % direct_leaf)
            continue

        for axis in "XYZ":
            source_plug = source_corrective + ".translate" + axis
            if not _safe_key_count(source_plug):
                continue
            target_plug = target_corrective + ".translate" + axis
            report["keys"] += _copy_curve(source_plug, target_plug,
                                           negate=(axis in negate_axes))
            report["translates"].append((source_plug, target_plug))


def mirror_source_joints(source_joints, direction="L_TO_R", include_multipliers=True,
                         include_translates=True, negate_axes=()):
    """Mirror corrective animation for selected source joints.

    ``source_joints`` should be source joints such as ``Knee_L`` and ``Elbow_L``.
    The result contains copied curve counts and skipped/missing target details.
    """
    report = {"keys": 0, "attributes": [], "translates": [], "missing": [], "sources": []}
    source_joints = list(source_joints or [])
    if not source_joints:
        raise RuntimeError(u"请先选择源关节，例如 Knee_L、Elbow_L。")

    for source_joint in source_joints:
        if not cmds.objExists(source_joint):
            report["missing"].append(u"源关节不存在：%s" % source_joint)
            continue
        source_joint = (cmds.ls(source_joint, long=True) or [source_joint])[0]
        pair = _side_pair_from_name(source_joint, direction)
        if not pair:
            expected = "_L" if direction == "L_TO_R" else "_R"
            report["missing"].append(u"源关节不包含 %s 标识：%s" % (expected, source_joint))
            continue
        source_side, target_side = pair
        report["sources"].append(source_joint)
        if include_multipliers:
            _mirror_multiplier_attributes(source_joint, source_side, target_side, report)
        if include_translates:
            _mirror_corrective_joint_translates(source_joint, source_side, target_side,
                                                set(negate_axes), report)
    return report


class CorrectiveKeyframeMirrorUI(object):
    def __init__(self):
        self.direction = None
        self.multipliers = None
        self.transforms = None
        self.negate_x = None
        self.negate_y = None
        self.negate_z = None
        self.status = None

    def show(self):
        if cmds.window(WINDOW_NAME, exists=True):
            cmds.deleteUI(WINDOW_NAME)
        # The original compact height could hide the default-source button and
        # the result report on 100% Windows scaling.
        cmds.window(WINDOW_NAME, title=WINDOW_TITLE, sizeable=True, widthHeight=(620, 430))
        cmds.columnLayout(adjustableColumn=True, rowSpacing=8)
        cmds.text(label=u"左→右时请选择 Knee_L、Elbow_L；右→左时请选择 Knee_R、Elbow_R。",
                  align="left")
        cmds.separator(height=8, style="in")
        self.direction = cmds.optionMenuGrp(label=u"镜像方向", columnWidth2=(120, 340))
        cmds.menuItem(label=u"左侧 → 右侧（_L → _R）")
        cmds.menuItem(label=u"右侧 → 左侧（_R → _L）")

        cmds.frameLayout(label=u"镜像内容", collapsable=False, marginWidth=10, marginHeight=8)
        self.multipliers = cmds.checkBox(
            label=u"镜像根修型 / 位移倍率属性关键帧（quatCorrectiveWeight、qc...MoveMultiplier）",
            value=True,
        )
        self.transforms = cmds.checkBox(label=u"镜像修型骨骼 translateX / Y / Z 关键帧", value=True)
        cmds.setParent("..")

        cmds.frameLayout(label=u"位移数值镜像（仅 transform 曲线）", collapsable=False,
                         marginWidth=10, marginHeight=8)
        cmds.text(label=u"只有左右本地轴数值需要反向时才勾选；标准修型骨骼通常保持不勾选。",
                  align="left")
        cmds.rowLayout(numberOfColumns=3, adjustableColumn=3, columnWidth3=(150, 150, 150))
        self.negate_x = cmds.checkBox(label=u"translateX 取反", value=False)
        self.negate_y = cmds.checkBox(label=u"translateY 取反", value=False)
        self.negate_z = cmds.checkBox(label=u"translateZ 取反", value=False)
        cmds.setParent("..")
        cmds.setParent("..")

        cmds.button(label=u"镜像所选源关节的关键帧", height=38, command=self._mirror_selected)
        cmds.button(label=u"直接镜像默认 Knee_L / Elbow_L（不依赖当前选择）", height=34,
                    command=self._mirror_defaults)
        self.status = cmds.scrollField(editable=False, wordWrap=True, height=54,
                                       text=u"准备就绪。工具不会在没有源关键帧时创建空动画。")
        cmds.showWindow(WINDOW_NAME)

    def _settings(self):
        label = cmds.optionMenuGrp(self.direction, query=True, value=True)
        direction = "L_TO_R" if label.startswith(u"左侧") else "R_TO_L"
        negate_axes = [axis for axis, control in (("X", self.negate_x), ("Y", self.negate_y), ("Z", self.negate_z))
                       if cmds.checkBox(control, query=True, value=True)]
        return direction, negate_axes

    def _run(self, source_joints):
        direction, negate_axes = self._settings()
        try:
            cmds.undoInfo(openChunk=True, chunkName="MirrorCorrectiveKeyframes")
            report = mirror_source_joints(
                source_joints,
                direction=direction,
                include_multipliers=cmds.checkBox(self.multipliers, query=True, value=True),
                include_translates=cmds.checkBox(self.transforms, query=True, value=True),
                negate_axes=negate_axes,
            )
        except Exception as exc:
            cmds.warning(str(exc))
            cmds.scrollField(self.status, edit=True, text=u"镜像失败：%s" % exc)
            return
        finally:
            cmds.undoInfo(closeChunk=True)

        detail = []
        detail.append(u"已复制 %d 个关键帧。" % report["keys"])
        detail.append(u"倍率属性曲线：%d 条；修型骨骼位移曲线：%d 条。" %
                      (len(report["attributes"]), len(report["translates"])))
        if report["missing"]:
            detail.append(u"跳过：\n" + u"\n".join(report["missing"]))
        if not report["keys"]:
            detail.append(u"没有发现可镜像的源关键帧。")
        message = u"\n".join(detail)
        cmds.scrollField(self.status, edit=True, text=message)
        cmds.inViewMessage(amg=u"<hl>修型关键帧镜像完成</hl><br>%s" % detail[0], pos="midCenterTop", fade=True)

    def _mirror_selected(self, *_):
        selection = cmds.ls(selection=True, long=True, type="joint") or []
        self._run(selection)

    def _mirror_defaults(self, *_):
        direction, _ = self._settings()
        expected = ("Knee_L", "Elbow_L") if direction == "L_TO_R" else ("Knee_R", "Elbow_R")
        joints = []
        for name in expected:
            joint = _node_from_short_name(name, node_type="joint")
            if joint:
                joints.append(joint)
        self._run(joints)


_UI = None


def show():
    global _UI
    _UI = CorrectiveKeyframeMirrorUI()
    _UI.show()
    return _UI
