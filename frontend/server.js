const { createServer } = require('https');
const { parse } = require('url');
const next = require('next');
const fs = require('fs');
const path = require('path');

const dev = process.env.NODE_ENV !== 'production';
const app = next({ dev });
const handle = app.getRequestHandler();

const httpsOptions = {
  key: fs.readFileSync(path.join(__dirname, 'certs/divalab-study.cci.key')),
  cert: fs.readFileSync(path.join(__dirname, 'certs/divalab-study_cci_drexel_edu_cert.cer')),
  ca: fs.readFileSync(path.join(__dirname, 'certs/divalab-study_cci_drexel_edu_interm.cer'))
};

app.prepare().then(() => {
  createServer(httpsOptions, (req, res) => {
    const parsedUrl = parse(req.url, true);
    handle(req, res, parsedUrl);
  }).listen(443, (err) => {
    if (err) throw err;
    console.log('> Ready on https://divalab-study.cci.drexel.edu:443');
  });
});