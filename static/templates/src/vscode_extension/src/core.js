// Pure logic: no `vscode` here.
function stripFences(text) {
  return text.replace(/```[\s\S]*?```/g, " ");
}

function countWords(text, opts) {
  let t = String(text || "");
  if (opts && opts.excludeCode) t = stripFences(t);
  const words = t.match(/[\p{L}\p{N}][\p{L}\p{N}'_-]*/gu);
  return words ? words.length : 0;
}

module.exports = { countWords, stripFences };
