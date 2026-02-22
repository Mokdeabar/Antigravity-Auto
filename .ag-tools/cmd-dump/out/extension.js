"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = require("vscode");
const fs = require("fs");
const path = require("path");
function activate(context) {
    console.log('[AG Command Dump] Extension activated, fetching all commands...');
    // Get ALL commands including internal ones
    vscode.commands.getCommands(true).then((commands) => {
        // Sort alphabetically for easy reference
        commands.sort();
        const header = `# AG Command Dump\n# Generated: ${new Date().toISOString()}\n# Total commands: ${commands.length}\n\n`;
        const content = header + commands.join('\n') + '\n';
        // Write to workspace root
        const workspaceFolders = vscode.workspace.workspaceFolders;
        let outputPath;
        if (workspaceFolders && workspaceFolders.length > 0) {
            outputPath = path.join(workspaceFolders[0].uri.fsPath, 'ag-commands.txt');
        }
        else {
            // Fallback: write next to the extension
            outputPath = path.join(context.extensionPath, '..', '..', 'ag-commands.txt');
        }
        fs.writeFileSync(outputPath, content, 'utf-8');
        console.log(`[AG Command Dump] Wrote ${commands.length} commands to: ${outputPath}`);
        vscode.window.showInformationMessage(`AG Command Dump: ${commands.length} commands written to ag-commands.txt`);
    });
}
function deactivate() { }
//# sourceMappingURL=extension.js.map