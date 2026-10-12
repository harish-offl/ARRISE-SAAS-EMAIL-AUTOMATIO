// Local development static file server
const fs   = require("fs");
const path = require("path");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css":  "text/css; charset=utf-8",
  ".js":   "application/javascript; charset=utf-8",
  ".json": "application/json",
  ".svg":  "image/svg+xml",
  ".ico":  "image/x-icon",
};

module.exports = function handler(req, res) {
  const url  = (req.url || "/").split("?")[0];
  const base = path.join(__dirname, "public");
  const file = url === "/" ? path.join(base, "index.html") : path.join(base, url.replace(/^\//, ""));

  if (fs.existsSync(file) && fs.statSync(file).isFile()) {
    const ext  = path.extname(file).toLowerCase();
    res.setHeader("Content-Type", MIME[ext] || "application/octet-stream");
    return res.status(200).send(fs.readFileSync(file));
  }

  // SPA fallback
  const index = path.join(base, "index.html");
  if (fs.existsSync(index)) {
    res.setHeader("Content-Type", "text/html; charset=utf-8");
    return res.status(200).send(fs.readFileSync(index));
  }

  res.status(404).send("Not found");
};
