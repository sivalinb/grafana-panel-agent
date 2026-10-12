import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
const pw = process.env.PLAYWRIGHT_MODULE_PATH
  ? await import(pathToFileURL(path.join(process.env.PLAYWRIGHT_MODULE_PATH,"index.mjs")).href)
  : await import("playwright");
const base=process.env.GUARDIAN_BASE_URL || "http://127.0.0.1:8787";
const output=path.resolve(process.env.DEMO_ARTIFACT_DIR || "docs/demo");
await fs.mkdir(output,{recursive:true});
const browser=await pw.chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE ? {executablePath:process.env.CHROME_EXECUTABLE} : {})});
const context=await browser.newContext({viewport:{width:1440,height:1050}});
const page=await context.newPage();
const errors=[];page.on("pageerror",error=>errors.push(error.message));
const api=async(route,body)=>{
  const response=await context.request[body===undefined ? "get" : "post"](base+route,body===undefined ? {} : {data:body});
  assert.ok(response.ok(), `${route}: ${await response.text()}`);
  return response.json();
};
async function idle() {
  for(let n=0;n<100;n++) {
    const state=await api("/api/state");
    if(!state.jobs.some(j=>j.status==="running")) {
      const failed=state.jobs.filter(j=>j.status==="failed").at(-1);
      if(failed && !failed.expected) throw new Error(`${failed.name}: ${failed.error}`);
      return state;
    }
    await page.waitForTimeout(500);
  }
  throw new Error("Agent operation did not finish");
}
try {
  await page.goto(base,{waitUntil:"networkidle"});
  await page.locator("#app").waitFor({state:"visible"});
  await idle();
  await page.getByRole("button",{name:"Reset demo"}).click();
  let state=await idle();
  assert.equal(state.checks.length,4);
  assert.equal(state.checks.find(c=>c.panel_id===1).state,"healthy");
  assert.equal(state.checks.find(c=>c.panel_id===4).state,"expected_empty");
  await page.waitForTimeout(1800);
  await page.screenshot({path:path.join(output,"01-healthy.png"),fullPage:true});
  console.log("PASS healthy baseline and intentionally empty panel");
  await page.getByRole("button",{name:"Simulate label migration"}).click();
  state=await idle();
  const incident=state.incidents.find(i=>i.status==="awaiting_review");
  assert.ok(incident,"Missing incident");
  assert.equal(state.checks.find(c=>c.panel_id===1).state,"empty");
  assert.ok(incident.proposal.after.includes('service="$service"'));
  assert.ok(incident.proposal.after.includes('environment="$environment"'));
  assert.ok(incident.screenshot_before.available,"Actual Grafana failure screenshot unavailable");
  await page.waitForTimeout(1800);
  await page.locator("#incident-cards .open-incident").first().click();
  await page.locator("#approve").waitFor({state:"visible"});
  await page.waitForTimeout(1800);
  await page.screenshot({path:path.join(output,"02-failure-review.png"),fullPage:true});
  console.log("PASS detection, evidence capture, and policy-validated diagnosis");
  await page.getByRole("button",{name:"Review and approve repair"}).click();
  await page.getByLabel("Reviewer note (optional)").fill("Synthetic demo review: validated checkout service, production environment and live candidate results.");
  await page.screenshot({path:path.join(output,"03-approval.png"),fullPage:true});
  await page.getByRole("button",{name:"Approve and apply",exact:true}).click();
  state=await idle();
  const resolved=state.incidents.find(i=>i.id===incident.id);
  assert.equal(resolved.status,"resolved");
  assert.equal(resolved.approved_by,"demo-reviewer");
  assert.equal(resolved.verification.state,"healthy");
  assert.ok(resolved.screenshot_after.available);
  const detail=await api(`/api/incidents/${incident.id}`);
  assert.ok(detail.events.some(e=>e.kind==="approved"));
  assert.ok(detail.events.some(e=>e.kind==="patch_applied"));
  assert.ok(detail.events.some(e=>e.kind==="recovery_verified"));
  assert.ok(detail.events.some(e=>e.kind==="rca_saved"));
  await page.waitForTimeout(1800);
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(output,"04-recovered.png"),fullPage:true});
  await page.screenshot({path:path.join(output,"04-recovered-workspace.png"),fullPage:false});
  const response=await context.request.get(base+`/api/incidents/${incident.id}/rca`);
  const rca=await response.text();
  assert.ok(rca.includes("repair_verified"));
  await fs.writeFile(path.join(output,"sample-rca.md"),rca);
  await fs.writeFile(path.join(output,"incident.json"),JSON.stringify(detail,null,2));
  for(const kind of ["before","after"]) {
    const shot=await context.request.get(base+"/api/evidence/"+detail[`screenshot_${kind}`].path);
    await fs.writeFile(path.join(output,`panel-${kind}.png`),await shot.body());
  }
  await page.getByRole("button",{name:"RCA history"}).click();
  await page.screenshot({path:path.join(output,"05-rca-history.png"),fullPage:true});
  assert.deepEqual(errors,[],"Browser JavaScript errors");
  await fs.writeFile(path.join(output,"verification.json"),JSON.stringify({at:new Date().toISOString(),grafana:"12.3.6",prometheus:"3.15.0",incident_id:incident.id,checks:["healthy baseline","intentional empty panel ignored","real label migration","empty panel detected","actual Grafana screenshot","live candidate query","explicit demo reviewer approval","Grafana dashboard saved","fresh recovery verification","RCA persisted","no browser JavaScript errors"],advisor:"No live model credentials supplied; evidence-based diagnosis used"},null,2));
  console.log("PASS explicit approval, real Grafana repair, recovery verification and RCA persistence");
  console.log("Demo artifacts: "+output);
} finally {await browser.close();}
