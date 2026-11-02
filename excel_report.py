"""Portable, dependency-free OOXML report writer for the CLI's tabular exports."""
from datetime import datetime
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED


def col(n):
    result = ''
    while n:
        n, r = divmod(n-1, 26)
        result = chr(65+r) + result
    return result


def write_xlsx(path, sheets):
    ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    types = ['<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
             '<Default Extension="xml" ContentType="application/xml"/>',
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
             '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    styles = f'''<styleSheet xmlns="{ns}"><fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="18"/><color rgb="FF17365D"/><name val="Calibri"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF17365D"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEAF1F8"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/><xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="3" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"><alignment vertical="center"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    def cell(value, row, column, style=0):
        ref = f'{col(column)}{row}'
        if isinstance(value, (int, float)):
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        text = ''.join(c for c in str(value) if ord(c)>=32 or c in '\n\t')[:32767]
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'
    with ZipFile(path, 'w', ZIP_DEFLATED) as z:
        workbook, relationships = [], []
        for index, sheet in enumerate(sheets, 1):
            name, title, note, headers, rows, widths = sheet
            last = col(len(headers))
            data = [f'<row r="1" ht="32" customHeight="1">{cell(title,1,1,2)}</row>',
                    f'<row r="2" ht="48" customHeight="1">{cell(note,2,1)}</row>',
                    '<row r="4" ht="42" customHeight="1">'+''.join(cell(h,4,i,1) for i,h in enumerate(headers,1))+'</row>']
            for r, values in enumerate(rows,5):
                data.append(f'<row r="{r}" ht="36" customHeight="1">'+''.join(cell(v,r,c,4 if isinstance(v,(int,float)) else (3 if r%2 else 0)) for c,v in enumerate(values,1))+'</row>')
            columns=''.join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i,w in enumerate(widths,1))
            end=max(4,len(rows)+4)
            xml=f'''<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane ySplit="4" topLeftCell="A5" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="20"/><cols>{columns}</cols><sheetData>{''.join(data)}</sheetData><autoFilter ref="A4:{last}{end}"/><mergeCells count="2"><mergeCell ref="A1:{last}1"/><mergeCell ref="A2:{last}2"/></mergeCells><pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/></worksheet>'''
            z.writestr(f'xl/worksheets/sheet{index}.xml',xml)
            types.append(f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
            workbook.append(f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>')
            relationships.append(f'<Relationship Id="rId{index}" Type="{relns}/worksheet" Target="worksheets/sheet{index}.xml"/>')
        relationships.append(f'<Relationship Id="rId{len(sheets)+1}" Type="{relns}/styles" Target="styles.xml"/>')
        z.writestr('xl/styles.xml',styles)
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{relns}"><sheets>{"".join(workbook)}</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(relationships)+'</Relationships>')
        z.writestr('_rels/.rels',f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{relns}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'+''.join(types)+'</Types>')
