import sys, json
p, LXmm, LYmm, ngap = float(sys.argv[1])*1e-3, float(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4])
out = sys.argv[5]
LX, LY, W, T = LXmm*1e-3, LYmm*1e-3, 2e-3, 35e-6
def poly(x0,y0,x1,y1): return [[x0,y0],[x1,y0],[x1,y1],[x0,y1]]
def dom(layers, c): return [{"shape_layer": layers, "shape_operation": "add", "shape_type": "polygon",
    "shape_data": {"buffer": 0.0, "coord_shell": c, "coord_holes": []}}]
geom = {"mesh_type":"shape","data_voxelize":{
  "param":{"dx":p,"dy":p,"dz":T,"cz":0.0,"simplify":1e-9,"construct":None,"xy_min":None,"xy_max":None},
  "layer_stack":[{"n_layer":1,"tag_layer":"bot"},{"n_layer":ngap,"tag_layer":"gap"},{"n_layer":1,"tag_layer":"top"}],
  "geometry_shape":{
    "plane": dom(["bot"], poly(0.0,-LY/2,LX,LY/2)),
    "trace": dom(["top"], poly(0.0,-W/2,LX,W/2)),
    "link":  dom(["bot","gap","top"], poly(LX-p,-W/2,LX,W/2)),
    "src":   dom(["top"], poly(0.0,-W/2,p,W/2)),
    "sink":  dom(["bot"], poly(0.0,-W/2,p,W/2))}},
  "data_point":{"check_cloud":True,"filter_cloud":True,"pts_cloud":[]},
  "data_resampling":{"use_reduce":True,"use_resample":False,"resampling_factor":[1,1,1]},
  "data_conflict":{"resolve_rules":True,"resolve_random":False,"conflict_rules":[
    {"domain_resolve":["trace"],"domain_keep":["src"]},
    {"domain_resolve":["plane"],"domain_keep":["sink"]},
    {"domain_resolve":["trace","plane"],"domain_keep":["link"]}]},
  "data_integrity":{"check_integrity":True,
    "domain_connected":{"conductor":{"domain_group":[["src"],["sink"],["trace"],["plane"],["link"]],"connected":True}},
    "domain_adjacent":{"terminal":{"domain_group":[["src"],["sink"]],"connected":False}}}}
json.dump(geom, open(out,"w"), indent=1)
