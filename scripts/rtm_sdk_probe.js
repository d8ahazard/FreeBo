const fs = require("fs");
const path = process.argv[2] || "webui/node_modules/agora-rtm-sdk/index.js";
const s = fs.readFileSync(path, "utf8");
const uniq = (a) => [...new Set(a)];
const urls = uniq(s.match(/[a-z0-9.\-]*agora\.io[a-z0-9.\-/]*/gi) || []);
console.log("AGORA hosts/urls:");
urls.slice(0, 50).forEach((u) => console.log("  ", u));
const wss = uniq(s.match(/wss?:\/\/[^"' ]+/gi) || []);
console.log("WS literals:");
wss.slice(0, 20).forEach((u) => console.log("  ", u));
const paths = uniq(s.match(/\/(ap|api|sd|rtm)\/[a-z0-9_/\-]+/gi) || []);
console.log("PATHS:");
paths.slice(0, 40).forEach((u) => console.log("  ", u));
// protobuf-ish field names / message names commonly in RTM
const kw = ["LoginRequest", "login", "PeerMessage", "sendMessageToPeer", "MessageType", "instructionId", "vid", "appId", "edge", "ap_", "voiceMonitor", "_protoc", "messagePb", "p2p", "DataStream"];
console.log("KEYWORDS present:");
kw.forEach((k) => { if (s.includes(k)) console.log("  ", k, "x" + (s.split(k).length - 1)); });
