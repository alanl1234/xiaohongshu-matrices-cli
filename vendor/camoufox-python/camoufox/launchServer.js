// Workaround that accesses Playwright's `launchServer` method in Python
// Without having to install the Node.js Playwright library.

const path = require('path')

// The driver shipped with playwright-python is a copy of playwright-core, so its
// entrypoint exposes `launchServer`. Resolve through the entrypoint rather than lib/
// internals, whose layout is private and changes between releases: 1.60 bundled
// lib/browserServerImpl.js away, which broke this script.
const driverPackage = process.argv[2]

let playwright
try {
    playwright = require(path.join(driverPackage, 'index.js'))
} catch (error) {
    console.error(`Error loading the Playwright driver from ${driverPackage}:`, error.message)
    process.exit(1)
}

function collectData() {
    return new Promise((resolve) => {
        let data = '';
        process.stdin.setEncoding('utf8');

        process.stdin.on('data', (chunk) => {
            data += chunk;
        });

        process.stdin.on('end', () => {
            resolve(JSON.parse(Buffer.from(data, "base64").toString()));
        });
    });
}

collectData().then((options) => {
    console.time('Server launched');
    console.info('Launching server...');
    
    playwright.firefox.launchServer(options).then(browserServer => {
        console.timeEnd('Server launched');
        console.log('Websocket endpoint:\x1b[93m', browserServer.wsEndpoint(), '\x1b[0m');
        // Continue forever
        process.stdin.resume();
    }).catch(error => {
        console.error('Error launching server:', error.message);
        process.exit(1);
    });
}).catch((error) => {
    console.error('Error collecting data:', error.message);
    process.exit(1);  // Exit with error code
});
