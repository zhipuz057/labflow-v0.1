"""Conservative, local purchase-text parsing using only the standard library."""
import re

LABELS = {'产品名称': 'item_name', '物品名称': 'item_name', '名称': 'item_name',
          '品牌': 'brand', '货号': 'catalog_no', '规格': 'specification',
          '单价': 'unit_price', '报价': 'unit_price', '价格': 'unit_price',
          '数量': 'quantity', '供应商': 'supplier'}
NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
SPEC = re.compile(r'(?<![\w.])\d+(?:\.\d+)?\s*(?:μl|µl|ul|ml|mg|kg|ug|μg|g|tests?|T)(?![A-Za-z])', re.I)
CATALOG = re.compile(r'(?<![\w-])(?:[A-Za-z]{1,8}-?\d+[A-Za-z0-9-]*|\d{3,}[A-Za-z][A-Za-z0-9-]*)(?![\w-])')
BRANDS = re.compile(r'(?<![A-Za-z])(?:MedChemExpress|Cell Signaling Technology|Thermo Fisher|Sigma-Aldrich|Beyotime|Abcam|Servicebio|碧云天|CST|MCE|Sigma|Thermo|Invitrogen|华兴|索莱宝)(?![A-Za-z])', re.I)


def normalize_spec(value):
    def replace(match):
        number, unit = re.match(r'(\d+(?:\.\d+)?)\s*(.*)', match.group()).groups()
        unit = {'ul': 'μL', 'μl': 'μL', 'µl': 'μL', 'ml': 'ml', 'mg': 'mg',
                'kg': 'kg', 'ug': 'μg', 'μg': 'μg', 'g': 'g', 't': 'T',
                'test': 'tests', 'tests': 'tests'}[unit.lower()]
        return f'{number} {unit}'
    return SPEC.sub(replace, value.strip())


def parse_purchase_text(text: str) -> dict:
    result = dict(item_name='', brand='', catalog_no='', specification='',
                  unit_price=None, quantity=1, supplier='')
    text = text.strip()
    if not text:
        return result
    # Explicit labels take precedence; values end at the next label or separator.
    label_pattern = re.compile('(' + '|'.join(LABELS) + r')\s*[:：]\s*')
    matches = list(label_pattern.finditer(text))
    remaining = list(text)
    explicit = set()
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        segment = text[match.end():end]
        value = re.split(r'[\n;；，]|,(?!\d{3}(?:\D|$))', segment, maxsplit=1)[0].strip()
        key = LABELS[match.group(1)]
        explicit.add(key)
        if key == 'unit_price':
            price = re.fullmatch(r'[¥￥]?\s*(' + NUMBER + r')\s*(?:元)?(?:\s*/\s*[^\s]+)?', value)
            if price:
                result[key] = float(price.group(1).replace(',', ''))
        elif key == 'quantity':
            quantity = re.fullmatch(r'(\d+)\s*(?:瓶|盒|包|支|个|套|袋|件)?', value)
            if quantity and int(quantity.group(1)) > 0:
                result[key] = int(quantity.group(1))
        else:
            result[key] = normalize_spec(value) if key == 'specification' else value
        # Consume the label and its value only, leaving subsequent unlabelled text.
        consumed_end = match.end() + len(segment) - len(segment.lstrip()) + len(value)
        remaining[match.start():consumed_end] = ' ' * (consumed_end - match.start())
    rest = ''.join(remaining)

    def take(pattern, key, transform=lambda m: m.group()):
        nonlocal rest
        match = pattern.search(rest)
        if match:
            if key not in explicit:
                result[key] = transform(match)
            rest = rest[:match.start()] + ' ' + rest[match.end():]

    take(BRANDS, 'brand')
    take(SPEC, 'specification', lambda m: normalize_spec(m.group()))
    price_pattern = re.compile(r'(?:[¥￥]\s*|(?:单价|报价|价格)\s*[:：]?\s*[¥￥]?\s*)(' + NUMBER + r')\s*元?|(' + NUMBER + r')\s*元')
    take(price_pattern, 'unit_price', lambda m: float((m.group(1) or m.group(2)).replace(',', '')))
    take(re.compile(r'(?<![\w.])([1-9]\d*)\s*(?:瓶|盒|包|支|个|套|袋|件)(?![A-Za-z])'),
         'quantity', lambda m: int(m.group(1)))
    take(CATALOG, 'catalog_no')
    # An unlabelled name is accepted only within a recognizable brand/catalog line.
    candidate = rest.strip(' \t\n,，;；。')
    if ('item_name' not in explicit and result['brand'] and result['catalog_no']
            and candidate and not re.search(r'[:：\n¥￥]|报价|价格|供应商', candidate)):
        result['item_name'] = re.sub(r'\s+', ' ', candidate).strip()
    return result
