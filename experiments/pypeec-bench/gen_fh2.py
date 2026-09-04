import sys
p = float(sys.argv[1]); out = sys.argv[2]; spread = sys.argv[3] == "spread"
LX, LY, T, H, W = 40.0, 20.0, 0.08, 1.68, 2.0
s1 = int(round(LX/p)); s2 = int(round(LY/p)); n = int(round(LX/p))
ys = [round(-W/2 + i*p, 6) for i in range(int(round(W/p))+1)] if spread else [0.0]
L = [".units mm", ".default sigma=58000 nwinc=1 nhinc=1", ""]
L += ["g1 x1=0 y1=%g z1=0" % (-LY/2),
      "+  x2=%g y2=%g z2=0" % (LX, -LY/2),
      "+  x3=%g y3=%g z3=0" % (LX, LY/2),
      "+  thick=%g seg1=%d seg2=%d nhinc=1" % (T, s1, s2)]
for j, y in enumerate(ys):
    L.append("+  Np%d (0,%g,0)" % (j, y))
    L.append("+  Ns%d (%g,%g,0)" % (j, LX, y))
L.append("")
for i in range(n+1):
    L.append("Nt%d x=%g y=0 z=%g" % (i, i*LX/n, H))
for i in range(n):
    L.append("Et%d Nt%d Nt%d w=%g h=%g" % (i, i, i+1, W, T))
L.append("Nsh x=%g y=0 z=0" % LX)
L.append("Elink Nt%d Nsh w=%g h=%g wx=0 wy=1 wz=0" % (n, W, p))
L.append(".equiv Nsh " + " ".join("Ns%d" % j for j in range(len(ys))))
if len(ys) > 1:
    L.append(".equiv " + " ".join("Np%d" % j for j in range(len(ys))))
L += ["", ".external Nt0 Np0", ".freq fmin=1e6 fmax=1e6 ndec=1", ".end"]
open(out, "w").write("\n".join(L) + "\n")
