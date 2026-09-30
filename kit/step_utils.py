# -*- coding: utf-8 -*-
"""
step_utils.py —— STEP/IGES → 三角网格转换(OCCT 绑定,独立版)。

协议:与 mfcad_graph_kit mesh_graph._step_to_trimesh 完全一致,
      tessellation 参数(deflection_ratio=800, angular=0.5)与
      geom_pipeline_kit 同源。

OCCT 绑定二选一(模块名一致,自动探测):
  - pythonocc-core: conda install -c conda-forge pythonocc-core
  - cadquery-ocp(OCP): pip install cadquery-ocp-novtk (支持 py3.10~3.14)

用法:
  from step_utils import step_to_trimesh
  mesh = step_to_trimesh('path/to/model.step')  # -> trimesh.Trimesh
"""

import os

import numpy as np

# STEP tessellation 协议(与 geom_pipeline_kit / 旧项目 step_to_obj.py 一致)
_DEFLECTION_RATIO = 800.0
_MESH_ANGULAR = 0.5


def step_to_trimesh(path):
    """STEP/IGES -> trimesh.Trimesh(原始坐标,未归一化)。

    返回原始坐标网格——归一化统一交给 shape 的 MeshPreprocessor 完成,
    避免双重归一化语义漂移。
    """
    import trimesh

    if not os.path.isfile(path):
        raise FileNotFoundError('STEP 文件不存在: %s' % path)

    # --- OCCT 绑定自动探测 ---
    try:
        from OCC.Core.BRep import BRep_Tool
        from OCC.Core.BRepMesh import BRepMesh_IncrementalMesh
        from OCC.Core.STEPControl import STEPControl_Reader
        from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_VERTEX
        from OCC.Core.TopExp import TopExp_Explorer
        from OCC.Core.TopLoc import TopLoc_Location
        from OCC.Core.TopoDS import topods

        def _topods_face(s):
            return topods.Face(s)

        def _topods_vertex(s):
            return topods.Vertex(s)

        def _vertex_pnt(v):
            return BRep_Tool.Pnt(v)

        def _triangulation(face, loc):
            return BRep_Tool.Triangulation(face, loc)
    except ImportError as e_occ:
        try:
            from OCP.BRep import BRep_Tool
            from OCP.BRepMesh import BRepMesh_IncrementalMesh
            from OCP.STEPControl import STEPControl_Reader
            from OCP.TopAbs import TopAbs_FACE, TopAbs_VERTEX
            from OCP.TopExp import TopExp_Explorer
            from OCP.TopLoc import TopLoc_Location
            from OCP.TopoDS import TopoDS

            def _pick(mod, *names):
                """新版 OCP(OCCT 7.9+)移除 _s 后缀,运行时兼容新旧命名。"""
                for n in names:
                    fn = getattr(mod, n, None)
                    if callable(fn):
                        return fn
                raise AttributeError('OCP 模块 %s 缺少成员: %s'
                                     % (mod.__name__, names))

            def _topods_face(s):
                return _pick(TopoDS, 'Face_s', 'Face')(s)

            def _topods_vertex(s):
                return _pick(TopoDS, 'Vertex_s', 'Vertex')(s)

            def _vertex_pnt(v):
                return _pick(BRep_Tool, 'Pnt_s', 'Pnt')(v)

            def _triangulation(face, loc):
                return _pick(BRep_Tool, 'Triangulation_s',
                             'Triangulation')(face, loc)
        except ImportError as e_ocp:
            # 透传两分支原始错误:只报笼统提示无法定位环境问题
            # (如 spawn 子进程导入 cadquery-ocp 失败的真正原因)
            raise ImportError(
                'STEP/IGES 输入需要 OCCT 绑定(pythonocc-core 或 '
                'cadquery-ocp),pip 安装: pip install cadquery-ocp-novtk\n'
                '  原始错误 -> OCC: %s\n  原始错误 -> OCP: %s'
                % (e_occ, e_ocp))

    # --- 读取 STEP ---
    reader = STEPControl_Reader()
    if reader.ReadFile(os.path.abspath(path)) != 1:
        raise ValueError('STEP 读取失败: %s' % path)
    reader.TransferRoots()
    shape = reader.OneShape()

    # --- tessellation(偏转量由包围盒对角线决定) ---
    # 注意:新版 OCP(OCCT 7.8+)Bnd_Box.Get() 返回未注册的 Bnd_Box::Limits
    # 结构,调用即抛 TypeError(Unregistered type);改用遍历 shape 顶点求
    # 包围盒,对 pythonOCC / OCP 全版本通用。
    xmin = ymin = zmin = np.inf
    xmax = ymax = zmax = -np.inf
    expv = TopExp_Explorer(shape, TopAbs_VERTEX)
    n_vert = 0
    while expv.More():
        p = _vertex_pnt(_topods_vertex(expv.Current()))
        x, y, z = p.X(), p.Y(), p.Z()
        xmin = min(xmin, x)
        ymin = min(ymin, y)
        zmin = min(zmin, z)
        xmax = max(xmax, x)
        ymax = max(ymax, y)
        zmax = max(zmax, z)
        n_vert += 1
        expv.Next()
    if n_vert == 0:
        raise ValueError('STEP 无顶点,无法求包围盒: %s' % path)
    diag = float(np.linalg.norm([xmax - xmin, ymax - ymin, zmax - zmin]))
    lin_def = diag / _DEFLECTION_RATIO
    BRepMesh_IncrementalMesh(shape, lin_def, False, _MESH_ANGULAR, True)

    # --- 收集三角面 ---
    verts = []
    faces = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = _topods_face(exp.Current())
        loc = TopLoc_Location()
        tri = _triangulation(face, loc)
        if tri is not None:
            trsf = loc.Transformation()
            n0 = len(verts)
            for i in range(1, tri.NbNodes() + 1):
                p = tri.Node(i)
                x, y, z = p.X(), p.Y(), p.Z()
                verts.append([
                    trsf.Value(1, 1) * x + trsf.Value(1, 2) * y +
                    trsf.Value(1, 3) * z + trsf.Value(1, 4),
                    trsf.Value(2, 1) * x + trsf.Value(2, 2) * y +
                    trsf.Value(2, 3) * z + trsf.Value(2, 4),
                    trsf.Value(3, 1) * x + trsf.Value(3, 2) * y +
                    trsf.Value(3, 3) * z + trsf.Value(3, 4)])
            for t in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(t).Get()
                faces.append([n0 + a - 1, n0 + b - 1, n0 + c - 1])
        exp.Next()

    if not faces:
        raise ValueError('STEP tessellation 失败(无三角面): %s' % path)

    return trimesh.Trimesh(vertices=np.asarray(verts, dtype=np.float64),
                           faces=np.asarray(faces, dtype=np.int64),
                           process=True)


def sample_surface(mesh, n_points, seed):
    """面积加权均匀采样三角网格表面,返回 (n_points, 3) float32。

    与 mfcad_graph_kit mesh_graph.sample_surface 协议一致。
    """
    tris = np.asarray(mesh.triangles, dtype=np.float64)     # (F, 3, 3)
    a = tris[:, 0]
    b = tris[:, 1] - a
    c = tris[:, 2] - a
    areas = 0.5 * np.linalg.norm(np.cross(b, c), axis=1)
    total = float(areas.sum())
    if total <= 0:
        raise ValueError('网格总面积为零')
    rng = np.random.RandomState(seed)
    face_idx = rng.choice(len(tris), size=n_points, p=areas / total)
    u = rng.uniform(0.0, 1.0, size=(n_points, 2))
    mask = u.sum(axis=1) > 1.0
    u[mask] = 1.0 - u[mask]
    pts = (a[face_idx] + u[:, :1] * b[face_idx] + u[:, 1:] * c[face_idx])
    return pts.astype(np.float32)