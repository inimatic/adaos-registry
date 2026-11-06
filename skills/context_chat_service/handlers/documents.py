"""Bounded local document extraction; never evaluates spreadsheet formulas."""
import io
import posixpath
import zipfile
import json
import subprocess
import sys
import re
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET


def markdown_text(value):
    return re.sub(r'([\\`*_{}\[\]<>|])', r'\\\1', value)


def docx_markdown(raw, maximum=160000):
    w = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    r = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            def xml(name):
                info = archive.getinfo(name)
                if info.file_size > 8*1024*1024:
                    raise ValueError('Документ слишком большой после распаковки')
                value = archive.read(info)
                if b'<!DOCTYPE' in value.upper() or b'<!ENTITY' in value.upper():
                    raise ValueError('Неподдерживаемая структура XML.')
                return ET.fromstring(value)
            names = set(archive.namelist())
            styles = {s.get(w+'styleId'): s for s in xml('word/styles.xml')} if 'word/styles.xml' in names else {}
            links = {e.get('Id'): e.get('Target', '') for e in xml('word/_rels/document.xml.rels')} if 'word/_rels/document.xml.rels' in names else {}
            bullets = set()
            if 'word/numbering.xml' in names:
                numbering = xml('word/numbering.xml')
                abstract = {n.get(w+'abstractNumId'): n for n in numbering.findall(w+'abstractNum')}
                for num in numbering.findall(w+'num'):
                    reference = num.find(w+'abstractNumId')
                    definition = abstract.get(reference.get(w+'val')) if reference is not None else None
                    if definition is not None:
                        for level in definition.findall(w+'lvl'):
                            fmt = level.find(w+'numFmt')
                            if fmt is not None and fmt.get(w+'val') == 'bullet':
                                bullets.add((num.get(w+'numId'), level.get(w+'ilvl', '0')))

            def inline(node):
                if node.tag == w+'t': return markdown_text(node.text or '')
                if node.tag == w+'tab': return '    '
                if node.tag == w+'br': return '  \n'
                if node.tag == w+'drawing': return '[Изображение]'
                text = ''.join(inline(child) for child in node)
                if node.tag == w+'r' and text.strip():
                    props = node.find(w+'rPr')
                    if props is not None:
                        for key, marker in [('b', '**'), ('i', '*'), ('strike', '~~')]:
                            value = props.find(w+key)
                            if value is not None and value.get(w+'val', '1') not in ('0','false','off'):
                                text = text[:len(text)-len(text.lstrip())] + marker + text.strip() + marker + text[len(text.rstrip()):]
                if node.tag == w+'hyperlink':
                    url = links.get(node.get(r+'id'), '')
                    if urlsplit(url).scheme in ('https','http','mailto') and not any(c in url for c in '<>\r\n'):
                        return '['+text+'](<'+url+'>)'
                return text

            def paragraph(node):
                text = inline(node).strip()
                if not text: return ''
                props = node.find(w+'pPr')
                style = props.find(w+'pStyle') if props is not None else None
                style_id = style.get(w+'val') if style is not None else ''
                chain, seen = [], set()
                while style_id in styles and style_id not in seen and len(chain) < 8:
                    seen.add(style_id); definition = styles[style_id]; chain.append(definition)
                    base = definition.find(w+'basedOn'); style_id = base.get(w+'val') if base is not None else ''
                for candidate in [props, *[s.find(w+'pPr') for s in chain]]:
                    level = candidate.find(w+'outlineLvl') if candidate is not None else None
                    if level is not None and level.get(w+'val', '').isdigit() and int(level.get(w+'val')) < 9:
                        return '#'*min(6, int(level.get(w+'val'))+1)+' '+text
                for definition in chain:
                    name = definition.find(w+'name')
                    match = re.fullmatch(r'(?:heading|заголовок)\s*([1-6])', name.get(w+'val',''), re.I) if name is not None else None
                    if match: return '#'*int(match[1])+' '+text
                num = props.find(w+'numPr') if props is not None else None
                if num is not None:
                    identity, level = num.find(w+'numId'), num.find(w+'ilvl')
                    key = (identity.get(w+'val') if identity is not None else '', level.get(w+'val','0') if level is not None else '0')
                    depth = min(8, int(key[1])) if key[1].isdigit() else 0
                    return '  '*depth+('- ' if key in bullets else '1. ')+text
                return re.sub(r'^([#>+-])(?=\s)', r'\\\1', text)

            def blocks(node):
                for child in node:
                    if child.tag == w+'p': yield paragraph(child)
                    elif child.tag == w+'tbl':
                        rows = [['<br>'.join(paragraph(p).replace('\n','<br>') for p in cell.findall(w+'p')) for cell in row.findall(w+'tc')] for row in child.findall(w+'tr')]
                        width = max(map(len, rows), default=0)
                        if width:
                            rows = [row+['']*(width-len(row)) for row in rows]
                            yield '\n'.join(['| '+' | '.join(rows[0])+' |', '| '+' | '.join(['---']*width)+' |', *['| '+' | '.join(row)+' |' for row in rows[1:]]])
                    else: yield from blocks(child)
            body = xml('word/document.xml').find(w+'body')
            text = '\n\n'.join(block for block in blocks(body) if block)
            if len(text) > maximum: raise ValueError('В документе больше 160 000 символов. Разделите его на части.')
            return text
    except ValueError: raise
    except (KeyError, TypeError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Не удалось прочитать DOCX') from exc


def pdf_text(raw):
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('pdf_worker.py'))], input=raw,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Обработка PDF заняла больше 30 секунд. Разделите документ на части.') from exc
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError('Не удалось обработать PDF в пределах памяти и времени. Разделите документ на части.')
    data = json.loads(result.stdout)
    if data.get('error'):
        raise ValueError(data['error'])
    return data['text']


def xlsx_text(raw, maximum=160000):
    ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) > 2048 or sum(e.file_size for e in entries) > 32 * 1024 * 1024:
                raise ValueError('Распакованная таблица слишком велика (до 32 МБ).')

            def xml(name):
                entry = archive.getinfo(name)
                if entry.file_size > 8 * 1024 * 1024:
                    raise ValueError('Лист таблицы слишком велик (до 8 МБ XML).')
                data = archive.read(entry)
                if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
                    raise ValueError('Неподдерживаемая структура XML.')
                return ET.fromstring(data)

            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(node.itertext()) for node in xml('xl/sharedStrings.xml').findall('s:si', ns)]
                if sum(map(len, strings)) > maximum:
                    raise ValueError('В таблице слишком много текста (до 160 000 символов).')
            relationships = {node.get('Id'): node for node in xml('xl/_rels/workbook.xml.rels')}
            chunks, size, cells = [], 0, 0

            def append(text):
                nonlocal size
                size += len(text) + 1
                if size > maximum:
                    raise ValueError('В таблице слишком много текста (до 160 000 символов).')
                chunks.append(text)

            for sheet in xml('xl/workbook.xml').findall('s:sheets/s:sheet', ns):
                relation = relationships[sheet.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
                if relation.get('TargetMode') == 'External':
                    raise ValueError('Внешние листы не поддерживаются.')
                target = relation.get('Target', '')
                name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else 'xl/' + target)
                if not name.startswith('xl/worksheets/') or not name.endswith('.xml'):
                    raise ValueError('Некорректный путь листа таблицы.')
                append('## Лист: ' + markdown_text(sheet.get('name', '')))
                append('| Ячейка | Значение |\n| --- | --- |')
                for row in xml(name).findall('s:sheetData/s:row', ns):
                    values = []
                    for cell in row.findall('s:c', ns):
                        cells += 1
                        if cells > 100000:
                            raise ValueError('В таблице слишком много ячеек (до 100 000).')
                        value = cell.findtext('s:v', '', ns)
                        if cell.get('t') == 's':
                            value = strings[int(value)]
                        elif cell.get('t') == 'inlineStr':
                            value = ''.join(cell.find('s:is', ns).itertext())
                        elif cell.get('t') == 'b':
                            value = 'TRUE' if value == '1' else 'FALSE'
                        if cell.find('s:f', ns) is not None and not value:
                            value = '[формула без сохранённого результата]'
                        if value:
                            values.append('| ' + markdown_text(cell.get('r') or '') + ' | ' + markdown_text(value).replace('\n', '<br>') + ' |')
                    if values:
                        append('\n'.join(values))
            return '\n'.join(chunks)
    except ValueError:
        raise
    except (KeyError, IndexError, TypeError, AttributeError, ET.ParseError, zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError('Не удалось прочитать XLSX. Сохраните таблицу заново или экспортируйте её в CSV.') from exc
