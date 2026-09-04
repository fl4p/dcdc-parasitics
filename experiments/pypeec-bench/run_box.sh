#!/bin/bash
SP="$1"; p="$2"; LX="$3"; LY="$4"; NG="$5"
python3 gen_box.py "$p" "$LX" "$LY" "$NG" gb.json || exit 1
S=$(python3 -c "import time;print(time.time())")
"$SP/.venv/bin/pypeec" -q mesher -ge gb.json -vo gb.pck > gm.log 2>&1
M=$(python3 -c "import time;print('%.1f'%(time.time()-$S))")
S2=$(python3 -c "import time;print(time.time())")
/usr/bin/time -l "$SP/.venv/bin/pypeec" -q solver -vo gb.pck -pr problem.yaml -to "$SP/ex/config/tolerance.yaml" -so gs.pck > gs.log 2> gt.log
python3 - "$p" "$LX" "$LY" "$NG" "$M" "$S2" <<'PY'
import sys,re,time
p,LX,LY,NG,M,S2=sys.argv[1:7]
w=time.time()-float(S2)
g=open('gm.log').read(); s=open('gs.log').read(); t=open('gt.log').read()
n=re.search(r'n = \((\d+), (\d+), (\d+)\)',g); u=re.search(r'n_used = (\d+)',g); tot=re.search(r'n_total = (\d+)',g)
rss=re.search(r'(\d+)\s+maximum resident set size',t)
print('box %sx%s mm pitch %s gap %s | grid %s | n_total=%s n_used=%s | mesh=%ss solve=%.1fs | peakRSS=%.2f GB | ok=%s'%(
 LX,LY,p,NG,n.group(0)[4:] if n else '?',tot.group(1) if tot else '?',u.group(1) if u else '?',M,w,
 int(rss.group(1))/2**30 if rss else float('nan'),'successful solver termination' in s))
if 'successful solver termination' not in s: print(s[-600:])
PY
