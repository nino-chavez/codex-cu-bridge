"""Manual smoke test: acts as Claude Code against the bridge and DRIVES Calculator on this Mac.

    python3 tests/smoke_calculator.py          # client without approval prompts (allow_app path)
    python3 tests/smoke_calculator.py elicit   # client with approval prompts (auto-accepts Calculator only)

Expect: Calculator shows 5 after typing 2+3=, only SkyComputerUseClient under app-server, clean exit.
"""
import os
import subprocess,json,sys,threading,time,queue,base64,re
ELICIT = "elicit" in sys.argv
b=subprocess.Popen([sys.executable,os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "codex_cu_bridge.py")],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
q={};nid=[0];lock=threading.Lock()
def w(o): b.stdin.write((json.dumps(o)+"\n").encode()); b.stdin.flush()
def reader():
    for l in b.stdout:
        m=json.loads(l)
        if "method" in m and "id" in m:
            msg=m["params"]["message"]; ok="Calculator" in msg
            print("  ELICITATION:",msg,"->","accept" if ok else "decline"); w({"jsonrpc":"2.0","id":m["id"],"result":{"action":"accept" if ok else "decline","content":{}}})
        elif "id" in m: q[m["id"]].put(m)
threading.Thread(target=reader,daemon=True).start()
def req(method,params=None,t=200):
    nid[0]+=1;i=nid[0];q[i]=queue.Queue(); w({"jsonrpc":"2.0","id":i,"method":method,"params":params or {}}); return q[i].get(timeout=t)
def call(tool,args,label):
    r=req("tools/call",{"name":tool,"arguments":args})["result"]
    txt=" ".join(c.get("text","") for c in r["content"] if c["type"]=="text")
    imgs=[c for c in r["content"] if c["type"]=="image"]
    val=re.search(r"Edit field, Value: ([^ |]*)",txt)
    print(f"{label}: isError={r.get('isError',False)} images={len(imgs)} display={val.group(1) if val else None}")
    if r.get("isError"): print("   ",txt[-400:])
    for k,im in enumerate(imgs): open(os.path.join(os.environ.get("TMPDIR","/tmp"),f"codex-cu-smoke-{label}.jpeg"),"wb").write(base64.b64decode(im["data"]))
    return r
caps={"elicitation":{}} if ELICIT else {}
r=req("initialize",{"protocolVersion":"2025-06-18","capabilities":caps,"clientInfo":{"name":"fake-claude","version":"0"}})
print("instructions present:", bool(r["result"].get("instructions")))
w({"jsonrpc":"2.0","method":"notifications/initialized"})
print("tools:",[t["name"] for t in req("tools/list")["result"]["tools"]])
r=call("get_app_state",{"app":"Calculator"},"read1")
if r.get("isError") and not ELICIT:
    call("allow_app",{"app":"Calculator"},"allow")
    call("get_app_state",{"app":"Calculator"},"read1b")
# descendants of codex app-server
out=subprocess.run(["ps","-axo","pid,ppid,command"],capture_output=True,text=True).stdout.splitlines()
procs={int(l.split()[0]):(int(l.split()[1]),l) for l in out[1:]}
root=[p for p,(pp,l) in procs.items() if pp==b.pid]
seen=set(root);st=list(root)
while st:
    x=st.pop()
    for p,(pp,l) in procs.items():
        if pp==x and p not in seen: seen.add(p);st.append(p)
print("bridge descendants:");[print("   ",procs[p][1][:150]) for p in sorted(seen)]
call("press_key",{"app":"Calculator","key":"Escape"},"clear")
call("type_text",{"app":"Calculator","text":"2+3="},"type")
call("get_app_state",{"app":"Calculator"},"read2")
b.stdin.close(); t0=time.time(); b.wait(timeout=20); print("bridge exited in %.1fs"%(time.time()-t0))
