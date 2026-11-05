const vscode = require("vscode");
const { countWords } = require("./core");

let item = null;
let visible = true;

function currentCount() {
  const ed = vscode.window.activeTextEditor;
  if (!ed) return null;
  const text = ed.selection.isEmpty ? ed.document.getText() : ed.document.getText(ed.selection);
  const excludeCode = vscode.workspace.getConfiguration("__PKG__").get("excludeCode", true)
    && ed.document.languageId === "markdown";
  return countWords(text, { excludeCode });
}

function refresh() {
  const n = currentCount();
  if (!visible || n === null) return item.hide();
  item.text = `$(book) ${n} words`;
  item.show();
}

function activate(context) {
  item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  item.command = "__PKG__.countWords";
  context.subscriptions.push(
    item,
    vscode.commands.registerCommand("__PKG__.countWords", () => {
      const n = currentCount();
      vscode.window.showInformationMessage(n === null ? "No open editor." : `${n} words`);
    }),
    vscode.commands.registerCommand("__PKG__.toggleStatus", () => { visible = !visible; refresh(); }),
    vscode.window.onDidChangeActiveTextEditor(refresh),
    vscode.window.onDidChangeTextEditorSelection(refresh),
    vscode.workspace.onDidChangeTextDocument(refresh)
  );
  refresh();
}

function deactivate() {}

module.exports = { activate, deactivate };
