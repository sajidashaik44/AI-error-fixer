import * as vscode from 'vscode';
import axios from 'axios';

interface ConsolidatedFix {
    primary_fix: string;
    primary_explanation: string;
    primary_confidence: number;
    alternative_fix: string;
    alternative_explanation: string;
    alternative_confidence: number;
    errors_fixed: string[];
    total_errors: number;
}

interface ConsolidatedFixResponse {
    consolidated_fix: ConsolidatedFix;
    processing_time: number;
    success: boolean;
}

interface BatchErrorRequest {
    errors: {
        error_message: string;
        code_snippet: string;
        line_number: number;
        error_id: string;
    }[];
    file_path: string;
}

export function activate(context: vscode.ExtensionContext) {
    console.log('🚀 Consolidated AI Error Fixer extension activated');

    let autoFixEnabled = vscode.workspace.getConfiguration('aiErrorFixer').get<boolean>('autoFixEnabled', true);

    // Status bar
    const statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'aiErrorFixer.toggleAutoFix';
    updateStatusBar();
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    function updateStatusBar() {
        statusBarItem.text = `$(zap) AI Fix: ${autoFixEnabled ? 'ON' : 'OFF'}`;
        statusBarItem.tooltip = autoFixEnabled ? 'Click to disable auto-fix' : 'Click to enable auto-fix';
    }

    async function getVSCodeDiagnostics(document: vscode.TextDocument): Promise<vscode.Diagnostic[]> {
        const diagnostics = vscode.languages.getDiagnostics(document.uri);
        return diagnostics.filter(diagnostic => 
            diagnostic.severity === vscode.DiagnosticSeverity.Error
        );
    }

    function getCleanCodeSnippet(document: vscode.TextDocument): string {
        // Return the entire document content without line numbers or markers
        return document.getText();
    }

    async function processAllErrors(document: vscode.TextDocument, editor: vscode.TextEditor) {
        try {
            console.log('🔍 Processing all errors with consolidated fix...');
            
            const diagnostics = await getVSCodeDiagnostics(document);
            
            if (diagnostics.length === 0) {
                console.log('No errors detected');
                vscode.window.showInformationMessage('No errors found in current file');
                return;
            }

            console.log(`📋 Processing ${diagnostics.length} errors for consolidated fix...`);

            // Get clean code snippet
            const cleanCodeSnippet = getCleanCodeSnippet(document);

            // Prepare batch request
            const errorRequests = diagnostics.map((diagnostic, index) => ({
                error_message: diagnostic.message,
                code_snippet: cleanCodeSnippet,
                line_number: diagnostic.range.start.line + 1,
                error_id: `error_${Date.now()}_${index}`
            }));

            const batchRequest: BatchErrorRequest = {
                errors: errorRequests,
                file_path: document.fileName
            };

            // Call consolidated API
            const apiUrl = vscode.workspace.getConfiguration('aiErrorFixer').get<string>('apiUrl', 'http://127.0.0.1:8000');
            
            console.log('🤖 Calling consolidated fix API...');
            const response = await axios.post(`${apiUrl}/fix-errors-consolidated`, batchRequest, {
                timeout: 120000,
                headers: { 'Content-Type': 'application/json' }
            });

            if (response.status === 200) {
                const consolidatedResponse: ConsolidatedFixResponse = response.data;
                console.log(`✅ Received consolidated fix for ${consolidatedResponse.consolidated_fix.total_errors} errors`);

                await showConsolidatedFixNotification(consolidatedResponse.consolidated_fix, editor);
            }

        } catch (error) {
            console.error('❌ Error processing consolidated fix:', error);
            if (axios.isAxiosError(error)) {
                if (error.code === 'ECONNREFUSED') {
                    vscode.window.showErrorMessage('❌ Cannot connect to AI API. Make sure the server is running.');
                } else if (error.code === 'ECONNABORTED') {
                    vscode.window.showErrorMessage('❌ AI API request timed out.');
                }
            }
        }
    }

    async function showConsolidatedFixNotification(consolidatedFix: ConsolidatedFix, editor: vscode.TextEditor) {
        const summaryMessage = `🤖 AI fixed ${consolidatedFix.total_errors} error${consolidatedFix.total_errors > 1 ? 's' : ''} (Primary: ${Math.round(consolidatedFix.primary_confidence * 100)}%, Alternative: ${Math.round(consolidatedFix.alternative_confidence * 100)}%)`;

        const action = await vscode.window.showInformationMessage(
            summaryMessage,
            { modal: false },
            'View Fixes',
            'Apply Primary Fix',
            'Apply Alternative Fix',
            'Dismiss'
        );

        switch (action) {
            case 'View Fixes':
                await showConsolidatedFixPanel(consolidatedFix, editor);
                break;
            case 'Apply Primary Fix':
                await applyConsolidatedFix(editor, consolidatedFix.primary_fix, 'Primary');
                break;
            case 'Apply Alternative Fix':
                await applyConsolidatedFix(editor, consolidatedFix.alternative_fix, 'Alternative');
                break;
        }
    }

    async function applyConsolidatedFix(editor: vscode.TextEditor, fixedCode: string, fixType: string) {
        const edit = new vscode.WorkspaceEdit();
        
        try {
            // Replace entire document content
            const fullRange = new vscode.Range(
                editor.document.positionAt(0),
                editor.document.positionAt(editor.document.getText().length)
            );
            
            edit.replace(editor.document.uri, fullRange, fixedCode);
            
            const success = await vscode.workspace.applyEdit(edit);
            
            if (success) {
                vscode.window.showInformationMessage(`✅ ${fixType} fix applied successfully!`);
            } else {
                vscode.window.showErrorMessage('❌ Failed to apply fix');
            }
            
        } catch (error) {
            console.error('❌ Error applying fix:', error);
            vscode.window.showErrorMessage(`❌ Error applying fix: ${error}`);
        }
    }

    async function showConsolidatedFixPanel(consolidatedFix: ConsolidatedFix, editor: vscode.TextEditor) {
        const panel = vscode.window.createWebviewPanel(
            'aiConsolidatedFix',
            `AI Consolidated Fix (${consolidatedFix.total_errors} errors)`,
            vscode.ViewColumn.Beside,
            { 
                enableScripts: true,
                retainContextWhenHidden: true
            }
        );

        panel.webview.onDidReceiveMessage(
            async message => {
                switch (message.command) {
                    case 'applyPrimaryFix':
                        await applyConsolidatedFix(editor, consolidatedFix.primary_fix, 'Primary');
                        break;
                    case 'applyAlternativeFix':
                        await applyConsolidatedFix(editor, consolidatedFix.alternative_fix, 'Alternative');
                        break;
                }
            },
            undefined,
            context.subscriptions
        );

        panel.webview.html = generateConsolidatedFixHTML(consolidatedFix);
    }

    function generateConsolidatedFixHTML(consolidatedFix: ConsolidatedFix): string {
        const primaryConfidenceColor = consolidatedFix.primary_confidence >= 0.8 ? '#059669' : 
                                      consolidatedFix.primary_confidence >= 0.6 ? '#d97706' : '#dc2626';
        
        const altConfidenceColor = consolidatedFix.alternative_confidence >= 0.8 ? '#059669' : 
                                  consolidatedFix.alternative_confidence >= 0.6 ? '#d97706' : '#dc2626';

        const errorsListHtml = consolidatedFix.errors_fixed.map(error => 
            `<li style="margin-bottom: 5px;">${error}</li>`
        ).join('');

        return `
            <!DOCTYPE html>
            <html>
            <head>
                <style>
                    body { 
                        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; 
                        margin: 20px; 
                        background-color: #f8f9fa;
                        line-height: 1.6;
                    }
                    
                    .header {
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        color: white;
                        padding: 20px;
                        border-radius: 12px;
                        margin-bottom: 20px;
                        text-align: center;
                    }
                    
                    .header h1 {
                        margin: 0 0 10px 0;
                        font-size: 24px;
                    }
                    
                    .errors-summary {
                        background: white;
                        padding: 20px;
                        border-radius: 8px;
                        margin-bottom: 20px;
                        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    }
                    
                    .errors-summary h3 {
                        margin-top: 0;
                        color: #1f2937;
                    }
                    
                    .errors-summary ul {
                        margin: 10px 0;
                        padding-left: 20px;
                    }
                    
                    .fix-container {
                        background: white;
                        border-radius: 12px;
                        padding: 20px;
                        margin-bottom: 20px;
                        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                    }
                    
                    .fix-header {
                        display: flex;
                        justify-content: space-between;
                        align-items: center;
                        margin-bottom: 15px;
                        flex-wrap: wrap;
                    }
                    
                    .fix-header h3 {
                        margin: 0;
                        color: #1f2937;
                        font-size: 18px;
                    }
                    
                    .confidence-badge { 
                        display: inline-block; 
                        padding: 6px 12px; 
                        border-radius: 20px; 
                        font-weight: 600;
                        font-size: 12px;
                        text-transform: uppercase;
                        letter-spacing: 0.5px;
                    }
                    
                    .apply-btn {
                        background: #3b82f6;
                        color: white;
                        border: none;
                        padding: 12px 24px;
                        border-radius: 6px;
                        font-weight: 600;
                        cursor: pointer;
                        font-size: 14px;
                        transition: all 0.2s;
                    }
                    
                    .apply-btn:hover {
                        background: #2563eb;
                        transform: translateY(-1px);
                    }
                    
                    .apply-btn.alternative {
                        background: #8b5cf6;
                    }
                    
                    .apply-btn.alternative:hover {
                        background: #7c3aed;
                    }
                    
                    .code-block { 
                        background: #1f2937;
                        color: #f9fafb;
                        padding: 16px; 
                        font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Roboto Mono', monospace; 
                        white-space: pre-wrap;
                        font-size: 13px;
                        line-height: 1.5;
                        overflow-x: auto;
                        border-radius: 6px;
                        margin: 15px 0;
                        max-height: 400px;
                        overflow-y: auto;
                    }
                    
                    .explanation { 
                        padding: 16px;
                        font-size: 14px;
                        line-height: 1.6;
                        background: #eff6ff;
                        color: #1e40af;
                        border-radius: 6px;
                        margin-top: 15px;
                    }
                    
                    .alternative .explanation {
                        background: #f3e8ff;
                        color: #6b21a8;
                    }
                </style>
            </head>
            <body>
                <div class="header">
                    <h1>🤖 Consolidated AI Fix</h1>
                    <p>Fixed ${consolidatedFix.total_errors} error${consolidatedFix.total_errors > 1 ? 's' : ''} with clean, ready-to-paste code</p>
                </div>

                <div class="errors-summary">
                    <h3>Errors Fixed:</h3>
                    <ul>
                        ${errorsListHtml}
                    </ul>
                </div>

                <div class="fix-container">
                    <div class="fix-header">
                        <h3>🎯 Primary Fix</h3>
                        <div>
                            <span class="confidence-badge" style="background-color: ${primaryConfidenceColor}20; color: ${primaryConfidenceColor}; margin-right: 10px;">
                                ${Math.round(consolidatedFix.primary_confidence * 100)}%
                            </span>
                            <button class="apply-btn" onclick="applyPrimaryFix()">
                                Apply Primary Fix
                            </button>
                        </div>
                    </div>
                    
                    <div class="code-block">${escapeHtml(consolidatedFix.primary_fix)}</div>
                    <div class="explanation">
                        ${consolidatedFix.primary_explanation}
                    </div>
                </div>

                <div class="fix-container alternative">
                    <div class="fix-header">
                        <h3>🔄 Alternative Fix</h3>
                        <div>
                            <span class="confidence-badge" style="background-color: ${altConfidenceColor}20; color: ${altConfidenceColor}; margin-right: 10px;">
                                ${Math.round(consolidatedFix.alternative_confidence * 100)}%
                            </span>
                            <button class="apply-btn alternative" onclick="applyAlternativeFix()">
                                Apply Alternative Fix
                            </button>
                        </div>
                    </div>
                    
                    <div class="code-block">${escapeHtml(consolidatedFix.alternative_fix)}</div>
                    <div class="explanation">
                        ${consolidatedFix.alternative_explanation}
                    </div>
                </div>

                <script>
                    const vscode = acquireVsCodeApi();
                    
                    function applyPrimaryFix() {
                        vscode.postMessage({
                            command: 'applyPrimaryFix'
                        });
                    }
                    
                    function applyAlternativeFix() {
                        vscode.postMessage({
                            command: 'applyAlternativeFix'
                        });
                    }
                </script>
            </body>
            </html>
        `;
    }

    function escapeHtml(text: string): string {
        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#039;');
    }

    // Commands
    const toggleAutoFix = vscode.commands.registerCommand('aiErrorFixer.toggleAutoFix', () => {
        autoFixEnabled = !autoFixEnabled;
        vscode.workspace.getConfiguration('aiErrorFixer').update('autoFixEnabled', autoFixEnabled, true);
        updateStatusBar();
        vscode.window.showInformationMessage(`AI Auto-Fix ${autoFixEnabled ? 'enabled' : 'disabled'}`);
    });

    const fixErrorCommand = vscode.commands.registerCommand('aiErrorFixer.fixError', async () => {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showWarningMessage('No active editor found');
            return;
        }

        const document = editor.document;
        if (document.languageId !== 'python') {
            vscode.window.showWarningMessage('AI Error Fixer currently supports Python files only');
            return;
        }

        await processAllErrors(document, editor);
    });

    // Event listeners
    const onDocumentSave = vscode.workspace.onDidSaveTextDocument(async (document) => {
        if (!autoFixEnabled || document.languageId !== 'python') {
            return;
        }

        const editor = vscode.window.visibleTextEditors.find(e => e.document === document);
        if (editor) {
            await processAllErrors(document, editor);
        }
    });

    context.subscriptions.push(
        toggleAutoFix,
        fixErrorCommand,
        onDocumentSave
    );

    console.log('✅ Consolidated AI Error Fixer extension fully activated');
}

export function deactivate() {
    console.log('👋 Consolidated AI Error Fixer extension deactivated');
}
