"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {healthy:"Healthy",empty:"No data",expected_empty:"Expected empty",unverified:"Unverified",error:"Query error",stale:"Stale data",missing_series:"Missing series",awaiting_review:"Needs review",resolved:"Resolved",applying:"Applying",verifying:"Verifying",rejected:"Rejected",repair_failed:"Repair failed",stale_approval:"Changed revision",closed:"Closed"};
const badge = (state) => `<span class="badge ${Object.hasOwn(labels,state) ? state : "unverified"}">${esc(labels[state] || state)}</span>`;
const date = (value) => value ? new Date(value).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}) : "—";
const when = (value) => value ? new Date(value).toLocaleString([], {month:"short",day:"numeric",hour:"2-digit",minute:"2-digit"}) : "—";
const safeURL = (value) => {try {const u = new URL(value); return ["http:","https:"].includes(u.protocol) ? esc(u.href) : "#";} catch {return "#";}};
let state = null, view = "health", selected = null, detail = null, approval = null, polling = false;
const knownJobs = new Set();

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
function toast(message) {$("#toast").textContent=message;$("#toast").hidden=false;}
async function action(path, body={}) {
  try {$("#toast").hidden=true; await api(path,body); await refresh();} catch(error) {toast(error.message);}
}
function showView(next) {
  view=next;
  const titles={health:["Dashboard health","Monitoring your monitoring.","Find missing telemetry. Review the evidence. Restore confidence."],review:["Review queue","Evidence first. Your decision.","Inspect the failure, review the patch, and verify the recovery."],history:["RCA history","A record of what changed.","Keep the evidence and turn monitoring failures into prevention actions."],about:["How it works","A reliable repair workflow.","Deterministic checks, bounded diagnosis, and a human decision."]};
  const [crumb,title,subtitle]=titles[view];
  $("#breadcrumb").textContent=crumb;$("#page-title").textContent=title;$("#page-subtitle").textContent=subtitle;
  for(const name of ["health","review","history","about"]) $(`#${name}-view`).hidden=name!==view;
  document.querySelectorAll(".nav").forEach(el=>el.classList.toggle("active",el.dataset.view===view));
  $("#metrics").hidden=view==="about";
  history.replaceState(null,"",`#${view}`);
  if(state) render();
}
function openIncident(id) {selected=id; detail=null; showView("review"); refresh();}
function empty(title,description) {return `<div class="empty-state wide-empty"><b>${esc(title)}</b>${esc(description)}</div>`;}
function render() {
  if(!state) return;
  const checks=state.checks;
  const reviews=state.incidents.filter(i=>i.status==="awaiting_review");
  const resolved=state.incidents.filter(i=>i.status==="resolved");
  const failing=checks.filter(c=>!["healthy","expected_empty","unverified"].includes(c.state));
  const healthy=checks.filter(c=>["healthy","expected_empty"].includes(c.state));
  const verified=checks.filter(c=>c.state!=="unverified").length;
  const busyJob=state.jobs.find(j=>j.status==="running");
  $("#operation").hidden=!busyJob;
  if(busyJob) $("#operation").textContent=`${busyJob.name} in progress. Evidence capture and verification may take a few seconds.`;
  $("#scan").disabled=!!busyJob;$("#simulate").disabled=!!busyJob;$("#reset-demo").disabled=!!busyJob;
  $("#queue-count").textContent=reviews.length;
  $("#mode").textContent=state.model_enabled ? "AI advisor enabled" : "Evidence-based diagnosis";
  $("#connection-label").textContent=state.last_error ? "Grafana needs attention" : "Connected to Grafana";
  $("#demo-banner").hidden=!state.demo;$("#simulate").hidden=!state.demo;
  $("#last-scan").textContent=state.last_run ? `Last scan ${date(state.last_run.at)} · Every ${state.scan_interval_seconds}s` : "Waiting for first scan";
  $("#coverage-count").textContent=`${verified}/${checks.length} supported contexts`;
  const metrics=[
    ["PANELS CHECKED",checks.length,`${new Set(checks.map(c=>c.uid)).size} dashboards · ${healthy.length} healthy`,"▦",""],
    ["PANELS NEEDING ATTENTION",failing.length,"Unexpected empty results or errors","△",failing.length ? "warning-metric" : ""],
    ["AWAITING YOUR REVIEW",reviews.length,"Exact patches. Explicit approval.","◇",reviews.length ? "warning-metric" : ""],
    ["VERIFIED RECOVERIES",resolved.length,"Evidence and RCA saved","✓","success-metric"]
  ];
  $("#metrics").innerHTML=metrics.map(([label,value,foot,icon,cls])=>`<div class="metric ${cls}"><span class="metric-label">${label}</span><span class="metric-icon">${icon}</span><div class="metric-value">${value}</div><div class="metric-foot">${esc(foot)}</div></div>`).join("");
  $("#checks").innerHTML=checks.length ? checks.map(check=>{
    const incident=state.incidents.find(i=>i.fingerprint===check.key && !["resolved","closed"].includes(i.status));
    return `<tr><td><div class="panel-name">${esc(check.panel)}</div><div class="panel-number">PANEL ${check.panel_id} · ${esc(check.owner)}</div></td><td>${esc(check.dashboard)}<div class="context">${esc(Object.entries(check.context).map(([k,v])=>`${k}: ${v}`).join(" · "))}</div></td><td>${badge(check.state)}${check.reason ? `<div class="context">${esc(check.reason)}</div>` : ""}</td><td class="subtle">${date(check.at)}</td><td>${incident ? `<button class="text-button open-incident" data-id="${incident.id}">Review →</button>` : ""}</td></tr>`;
  }).join("") : `<tr><td colspan="5">The first scheduled scan is loading your dashboard inventory.</td></tr>`;
  $("#incident-cards").innerHTML=state.incidents.length ? state.incidents.slice(0,3).map(item=>`<button class="incident-card open-incident" data-id="${item.id}"><div class="card-top">${badge(item.status)}<span class="subtle">${when(item.created_at)}</span></div><h3>${esc(item.panel_title)}</h3><p>${esc(item.diagnosis.cause)}</p><div class="card-footer"><span>${esc(item.dashboard_title)}</span><span>Inspect evidence →</span></div></button>`).join("") : empty("All checked panels are healthy",state.demo ? "Simulate a label migration to see the complete failure and repair workflow." : "New failures will appear here after the configured consecutive checks.");
  const incidents=state.incidents.filter(i=>i.status!=="closed");
  if(!selected && incidents.length) selected=(incidents.find(i=>i.status==="awaiting_review")||incidents[0]).id;
  $("#incident-list").innerHTML=incidents.length ? incidents.map(item=>`<button class="incident-list-item open-incident ${selected===item.id ? "selected" : ""}" data-id="${item.id}">${badge(item.status)}<strong>${esc(item.panel_title)}</strong><small>${esc(item.dashboard_title)}<br>${when(item.created_at)} · #${item.id}</small></button>`).join("") : empty("No investigations yet","A supported panel failure will create an evidence bundle here.");
  renderDetail(busyJob);
  $("#history-list").innerHTML=resolved.length ? resolved.map(item=>`<div class="history-item"><div>${badge(item.status)}<h3>${esc(item.panel_title)}</h3><p>${esc(item.diagnosis.cause)}</p><p>${when(item.resolved_at)} · ${esc(item.resolved_by)} · ${esc(item.diagnosis.cause_status)}</p></div><div class="history-actions"><button class="text-button open-incident" data-id="${item.id}">View evidence</button><a href="/api/incidents/${item.id}/rca" download="${item.id}-rca.md">Download RCA ↓</a></div></div>`).join("") : empty("Recovery history starts here","Resolved incidents retain their screenshots, decisions and RCA.");
  $("#advisor-mode").textContent=state.model_enabled ? "An optional model reads the bounded evidence bundle and provides cited advice. It has no write tools." : "The current run uses evidence-based rules without an LLM. Configure GUARDIAN_MODEL and OPENAI_API_KEY to enable the optional read-only AI advisor.";
  document.querySelectorAll(".open-incident").forEach(button=>button.onclick=()=>openIncident(button.dataset.id));
  if(state.last_error) toast(state.last_error);
  for(const job of state.jobs) {
    if(!knownJobs.has(job.id) && job.status==="failed") toast(`${job.name}: ${job.error}`);
    if(job.status!=="running") knownJobs.add(job.id);
  }
}
function renderDetail(busyJob) {
  const item=detail && detail.id===selected ? detail : state.incidents.find(i=>i.id===selected);
  if(!item) {$("#incident-detail").innerHTML=empty("Select an investigation","Review the original query, actual panel screenshot and suggested fix.");return;}
  const proposal=item.proposal;
  const resolved=item.status==="resolved";
  const steps=["Detected","Evidence","Diagnosed","Review","Repair","Verified","RCA"];
  const stage=resolved ? 7 : item.status==="verifying" ? 5 : item.status==="applying" ? 4 : 3;
  const shot=(kind,title)=>{
    const image=item[`screenshot_${kind}`];
    return `<div class="screenshot-card"><div class="screenshot-label"><span>${title}</span><span>ACTUAL GRAFANA CAPTURE</span></div>${image?.available ? `<a href="/api/evidence/${esc(image.path)}" target="_blank"><img src="/api/evidence/${esc(image.path)}" alt="${esc(item.panel_title)} ${kind==='before'?'showing the original failure':'after recovery'}"></a>` : `<div class="capture-missing">${esc(image?.reason||"Capture not available yet")}</div>`}</div>`;
  };
  let advice="";
  if(item.advisor?.advice) advice=`<h3>AI advisor · ${esc(item.advisor.model)}</h3><div class="finding"><p>${esc(item.advisor.advice.explanation)}</p><small>Cites ${esc(item.advisor.advice.evidence_ids.join(", "))}. Suggestions are advisory.</small></div>`;
  let actions="";
  if(item.status==="awaiting_review") actions=`${proposal ? `<button id="approve" class="button primary">Review and approve repair</button>` : ""}<button id="manual-verify" class="button secondary">Verify human fix</button><button id="reject" class="button danger">Reject proposal</button>`;
  else if(["rejected","stale_approval","repair_failed"].includes(item.status)) actions=`<button id="manual-verify" class="button secondary">Verify human fix</button><button id="close" class="button secondary">Close and allow rescan</button>`;
  else if(resolved) actions=`<a class="button secondary" href="/api/incidents/${item.id}/rca" download="${item.id}-rca.md">Download RCA ↓</a>`;
  $("#incident-detail").innerHTML=`<div class="detail-top"><h2>${esc(item.panel_title)}</h2>${badge(item.status)}</div><div class="detail-meta">${esc(item.dashboard_title)} · Panel ${item.panel_id} · Version ${item.dashboard_version} · #${item.id}<br><a href="${safeURL(item.panel_url)}" target="_blank" rel="noopener noreferrer">Open panel in Grafana ↗</a></div><div class="flow">${steps.map((step,i)=>`<span class="${i<stage ? "done" : i===stage ? "current" : ""}">${i<stage ? "✓ " : ""}${step}</span>`).join("")}</div><div class="finding"><span class="eyebrow">${esc(item.diagnosis.confidence)} · ${esc(item.diagnosis.cause_status)}</span><p>${esc(item.diagnosis.cause)}</p><small>${esc(item.diagnosis.reason)}</small></div><h3>Panel evidence</h3><div class="screenshot-grid ${!item.screenshot_after ? "single" : ""}">${shot("before","Before · unexpected no data")}${item.screenshot_after ? shot("after","After · recovery verified") : ""}</div><h3>Proposed query repair</h3>${proposal ? `<div class="diff"><div class="removed">− ${esc(proposal.before)}</div><div class="added">+ ${esc(proposal.after)}</div></div><div class="hash">APPROVAL HASH ${proposal.hash}</div><p class="subtle">Candidate passed the live panel contract. Service and environment filters are preserved.</p>` : `<p>No unique policy-validated repair. Investigate the evidence and apply a human fix.</p>`}${advice}<div class="actions">${actions}</div>${item.repair_error ? `<div class="finding"><p>${esc(item.repair_error)}</p></div>` : ""}<h3>Investigation evidence</h3>${item.evidence.map(e=>`<details class="evidence-item"><summary>${esc(e.id)} · ${esc(e.title)}</summary><pre>${esc(JSON.stringify(e.data,null,2))}</pre></details>`).join("")}${resolved ? `<h3>Recovery verification</h3><details class="evidence-item"><summary>Saved dashboard passed fresh query evaluations</summary><pre>${esc(JSON.stringify(item.verification,null,2))}</pre></details>` : ""}<h3>Activity timeline</h3><ul class="timeline">${(item.events||[]).map(e=>`<li><b>${esc(e.kind.replaceAll("_"," "))}</b><time>${date(e.at)}</time>${e.body.actor ? `<small>Reviewer: ${esc(e.body.actor)}</small>` : ""}${e.body.reason ? `<small>${esc(e.body.reason)}</small>` : ""}</li>`).join("")}</ul><h3>Prevent a repeat</h3><ul class="prevention">${item.diagnosis.prevention.map(p=>`<li>${esc(p)}</li>`).join("")}</ul>`;
  const btn=$("#approve");if(btn) btn.onclick=()=>{
    approval={id:item.id,hash:proposal.hash};
    $("#approval-diff").innerHTML=`<div class="removed">− ${esc(proposal.before)}</div><div class="added">+ ${esc(proposal.after)}</div>`;
    $("#approval-note").value="";$("#approval-dialog").showModal();
  };
  if($("#manual-verify")) $("#manual-verify").onclick=()=>action(`/api/incidents/${item.id}/verify`,{note:"Requested verification from the review workspace"});
  if($("#reject")) $("#reject").onclick=()=>action(`/api/incidents/${item.id}/reject`,{note:"Reviewer rejected the proposed repair"});
  if($("#close")) $("#close").onclick=()=>action(`/api/incidents/${item.id}/close`);
  $("#incident-detail").querySelectorAll("button").forEach(button=>button.disabled=!!busyJob);
}
async function refresh() {
  if(polling) return;
  polling=true;
  try {
    state=await api("/api/state");
    $("#app").hidden=false;$("#login").hidden=true;
    if(selected) detail=await api(`/api/incidents/${selected}`);
    render();
  } catch(error) {if(error.message.includes("Sign in")) {$("#login").hidden=false;$("#app").hidden=true;} else toast(error.message);}
  finally {polling=false;}
}
document.querySelectorAll(".nav").forEach(button=>button.onclick=()=>showView(button.dataset.view));
$("#scan").onclick=()=>action("/api/scan");
$("#simulate").onclick=()=>action("/api/demo/migrate");
$("#reset-demo").onclick=()=>action("/api/demo/reset");
$("#view-reviews").onclick=()=>showView("review");
$("#cancel-approval").onclick=()=>$("#approval-dialog").close();
$("#approval-form").onsubmit=async(event)=>{event.preventDefault();$("#approval-dialog").close();await action(`/api/incidents/${approval.id}/approve`,{proposal_hash:approval.hash,note:$("#approval-note").value});};
$("#login-form").onsubmit=async(event)=>{event.preventDefault();try {await api("/api/session",{token:$("#token").value});$("#token").value="";await refresh();} catch(error) {toast(error.message);}};
async function start() {
  const config=await api("/api/public-config");
  if(config.demo) await api("/api/session",{demo:true});
  const initial=location.hash.slice(1);showView(["health","review","history","about"].includes(initial) ? initial : "health");
  await refresh();setInterval(refresh,1500);
}
start().catch(error=>toast(error.message));
