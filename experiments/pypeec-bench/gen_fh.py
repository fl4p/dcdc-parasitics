import sys
p = float(sys.argv[1])            # mesh pitch in mm
LX, LY = 40.0, 20.0               # plane extent
T = 0.08                          # copper thickness
H = 1.68                          # trace centre height above plane centre
W = 2.0                           # trace width
s1 = int(round(LX/p)); s2 = int(round(LY/p))
n  = int(round(LX/p))             # trace segments
out = []
out.append("* trace over a %g x %g mm return plane, pitch %g mm" % (LX, LY, p))
out.append(".units mm")
out.append(".default sigma=58000 nwinc=1 nhinc=1")
out.append("")
# ground plane at z=0
out.append("g1 x1=0 y1=%g z1=0" % (-LY/2))
out.append("+  x2=%g y2=%g z2=0" % (LX, -LY/2))
out.append("+  x3=%g y3=%g z3=0" % (LX, LY/2))
out.append("+  thick=%g seg1=%d seg2=%d nhinc=1" % (T, s1, s2))
out.append("+  Nport (0,0,0)")
out.append("+  Nshort (%g,0,0)" % LX)
out.append("")
# trace, n segments
for i in range(n+1):
    out.append("Nt%d x=%g y=0 z=%g" % (i, i*LX/n, H))
for i in range(n):
    out.append("Et%d Nt%d Nt%d w=%g h=%g" % (i, i, i+1, W, T))
# vertical short
out.append("Nsh x=%g y=0 z=0" % LX)
out.append("Elink Nt%d Nsh w=%g h=%g wx=0 wy=1 wz=0" % (n, W, p))
out.append(".equiv Nsh Nshort")
out.append("")
out.append(".external Nt0 Nport")
out.append(".freq fmin=1e6 fmax=1e6 ndec=1")
out.append(".end")
open(sys.argv[2],"w").write("\n".join(out)+"\n")
print("pitch %g -> plane %dx%d segs, trace %d segs" % (p, s1, s2, n))
