import fs from 'node:fs/promises';
import path from 'node:path';
import { FileBlob, SpreadsheetFile } from '@oai/artifact-tool';

const [recordsPath, outputPath, templatePath = new URL('./template.xlsx', import.meta.url).pathname] = process.argv.slice(2);
if (!recordsPath || !outputPath) throw new Error('Usage: node xlsx.mjs records.json output.xlsx [template.xlsx]');
const doc = JSON.parse(await fs.readFile(recordsPath, 'utf8'));
if (!doc.reviewed || !doc.rows?.length) throw new Error('Сначала выполните converter.py compile');
if (new Set(doc.rows.map(r => r.case)).size !== doc.rows.length) throw new Error('Повторные номера');
function serial(value) {
  const match = /^(\d{2})\.(\d{2})\.(\d{4})$/.exec(value);
  if (!match) throw new Error(`Invalid date ${value}`);
  const [, day, month, year] = match.map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) throw new Error(`Invalid date ${value}`);
  return (date.getTime() - Date.UTC(1899, 11, 30)) / 86400000;
}
const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(templatePath));
const sheet = workbook.worksheets.getItemAt(0);
const end = doc.rows.length + 1;
const matrix = doc.rows.map(row => {
  const pages = row.pages ?? [row.page];
  const note = `Источник: NIAB_LF_${doc.fund}_1.pdf, стр. ${pages.join(', ')}.` + (row.note ? ` ${row.note}` : '');
  return [String(row.case), row.title, serial(row.start), serial(row.end), row.quantity ?? null, null, null, null, null, null, note];
});
sheet.getRange(`A2:K${end}`).values = matrix;
sheet.getRange(`A1:L${end}`).format.font = { name: 'Arial', size: 11, color: '#202020' };
sheet.getRange(`A1:L${end}`).format.verticalAlignment = 'top';
sheet.getRange('A1:K1').format = { font: { name: 'Arial', size: 11, bold: true, color: '#202020' }, fill: '#E8EDF1', wrapText: true, horizontalAlignment: 'center', verticalAlignment: 'center', rowHeight: 34 };
sheet.getRange(`A2:A${end}`).setNumberFormat('@');
sheet.getRange(`A2:A${end}`).format.horizontalAlignment = 'center';
sheet.getRange(`C2:D${end}`).setNumberFormat('dd.mm.yyyy');
sheet.getRange(`C2:E${end}`).format.horizontalAlignment = 'center';
sheet.getRange(`B2:B${end}`).format.wrapText = true;
sheet.getRange(`K2:K${end}`).format.wrapText = true;
sheet.getRange('L1').format.wrapText = true;
const widths = { A: 104, B: 540, C: 114, D: 114, E: 108, F: 92, G: 124, H: 42, I: 96, J: 74, K: 480, L: 270 };
for (const [col, width] of Object.entries(widths)) sheet.getRange(`${col}1:${col}${end}`).format.columnWidthPx = width;
sheet.getRange(`A2:K${end}`).format.autofitRows();
// Leave room for wrapping differences during Google Sheets' XLSX conversion.
function lines(text, capacity) {
  let count = 1, length = 0;
  for (const word of String(text).split(/\s+/)) {
    if (length && length + word.length + 1 > capacity) { count++; length = 0; }
    const fullLines = Math.floor(Math.max(0, word.length - 1) / capacity);
    count += fullLines;
    length += word.length - fullLines * capacity + (length ? 1 : 0);
  }
  return count;
}
for (let i = 0; i < matrix.length; i++) sheet.getRange(`A${i + 2}:K${i + 2}`).format.rowHeight = Math.max(26, 18 * Math.max(lines(matrix[i][1], 62), lines(matrix[i][10], 54)) + 8);
for (let i = 0; i < doc.rows.length; i++) {
  if (doc.rows[i].unresolved) {
    for (const col of ['B', 'C', 'D', 'K']) sheet.getRange(`${col}${i + 2}`).format.fill = '#FFF2CC';
  }
}
sheet.freezePanes.freezeRows(1);
workbook.recalculate();
await fs.mkdir(path.dirname(outputPath), { recursive: true });
if (process.env.NIAB_PREVIEW === '1') {
  for (const [label, range] of [['first', `A1:D${Math.min(end, 5)}`], ['last', `A${Math.max(2, end - 3)}:D${end}`]]) {
    const preview = await workbook.render({sheetName:sheet.name, range, scale:1, format:'png'});
    await fs.writeFile(`${outputPath}.${label}.png`, new Uint8Array(await preview.arrayBuffer()));
  }
}
await (await SpreadsheetFile.exportXlsx(workbook)).save(outputPath);
console.log(JSON.stringify({ fund:doc.fund, count:doc.rows.length, output:outputPath }));
