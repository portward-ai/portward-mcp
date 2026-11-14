// Runs the report page's scripts (the control app's report_static) under node as the browser runs them: classic
// scripts on the global object, with node's Web Crypto. Reads a JSON job from argv[3] and writes the result to stdout.
//   node report_in_node.js <report_static folder> <job.json>
// job: {private_jwk, public_key, sealed, account_id, request_id, records: [{id, version, ciphertext}], start, end}
// out: {code, opened: <records that opened>, csv, pdf (base64), reads, rows}
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const folder = process.argv[2];
for (const name of ["report_crypto.js", "report_build.js"]) {
  vm.runInThisContext(fs.readFileSync(path.join(folder, name), "utf8"), { filename: name });
}
const C = globalThis.PortwardReportCrypto;
const B = globalThis.PortwardReportBuild;

(async () => {
  const job = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
  const out = {};
  const publicRaw = C.unb64url(job.public_key);
  out.code = await C.code(publicRaw);
  if (job.sealed) {
    const privateKey = await crypto.subtle.importKey("jwk", job.private_jwk, { name: "ECDH", namedCurve: "P-256" },
                                                     false, ["deriveBits"]);
    const key = await C.openUnlock(privateKey, publicRaw, C.unb64url(job.sealed), job.account_id, job.request_id);
    const contents = await C.openRecords(key, job.account_id, job.records);
    out.opened = contents.length;
    const built = B.report(B.reads(contents, job.start, job.end), job.start, job.end);
    out.csv = Buffer.from(B.csvBytes(built)).toString("base64");
    out.pdf = Buffer.from(B.pdf(built)).toString("base64");
    out.reads = built.reads;
    out.rows = built.rows;
  }
  process.stdout.write(JSON.stringify(out));
})().catch((err) => {
  process.stderr.write(String(err && err.stack ? err.stack : err));
  process.exit(1);
});
