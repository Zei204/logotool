# logotool

`logotool.py` is a small command-line tool for inspecting, decoding and
re-encoding OnePlus/OPPO `LOGO` splash partition images (`SPLASH!!` RLE).
It is one file, uses only the Python standard library and Pillow, and never
flashes anything by itself.

The format description is in [FORMAT.md](FORMAT.md). The format was confirmed
on one OnePlus 5 dump; see the honest "verified / not verified" section below.

## Requirements

- Python 3.9 or newer
- Pillow (`python3 -m pip install Pillow`)

## Quick start

```sh
# Show the table: index, offset, size, slot, reserve, name, duplicate groups
python3 logotool.py info LOGO.img

# Decode to RGB PNG files plus out/manifest.json
python3 logotool.py decode LOGO.img out

# Edit the PNG files you care about. Keep the exact dimensions and save as RGB.

# Rebuild. Only changed blocks are replaced; all other bytes are copied as-is.
python3 logotool.py encode LOGO.img out --out LOGO_new.img
```

`decode` refuses to write into a non-empty directory unless `--force` is used.
`encode` refuses to overwrite the input image, and refuses to overwrite an
existing output file unless `--force` is used. Use `--dry-run` to run every
check and build the image without writing it.

### Selecting blocks

```sh
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only 0
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only fhd_oppo
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only 0,2 --dry-run
```

`--only` accepts an index, a full name, or a short name. If a block has
duplicates and they should stay identical, add `--apply-to-duplicates`.

### If the block no longer fits its slot

Image editors often quantise colours and introduce noise such as
`(252,253,253)` instead of pure white. That can make RLE twice as large and
the block no longer fits.

```sh
# Make pixels lighter than N white and all other pixels black.
python3 logotool.py encode LOGO.img out --out LOGO_new.img \
    --snap-threshold 200 --report
```

`--report` is only used when a block overflows. It shows the most expensive
40-row bands and the top colours in those bands.

### RGBA and palette PNGs

`encode` accepts exactly RGB PNGs. It does not silently convert RGBA or
palette images. For RGBA (or LA/PA), `--flatten` composites the image over a
black background first:

```sh
python3 logotool.py encode LOGO.img out --out LOGO_new.img --flatten
```

Palette PNGs should be re-saved as RGB. Use `--channel-order bgr|rgb` on
`decode` (or `encode`) if a different dump uses the other channel order.

## Artwork rules for the slot budget

- Use flat fills and hard edges.
- Do not use gradients, anti-aliasing, blur, or dithering.
- Make the background exactly `#000000` (`0,0,0`).
- Keep the exact PNG dimensions of the block you replace.
- Save as RGB, not RGBA or palette.
- Fewer colours and longer runs compress better.
- If an editor added near-white or near-black noise, use `--snap-threshold N`.
  This is a binary threshold: it intentionally turns the image into
  black-and-white, so do not use it on artwork that needs grey/anti-aliased
  edges.
- Use `--report` to find the rows that cost the most space.

## Verified / not verified

### Verified

- The format and RLE were checked on one OnePlus 5 dump:
  - 84 table entries, of which 60 are real blocks and 24 are empty records;
  - 1080x1920 panel images are present;
  - decompression and recompression match the dump;
  - decoding and re-encoding without edits produces a byte-identical image.
- Block 0 (the main logo) was flashed through `dd` and the logo changed on the
  device.

### Not verified

- Other devices and other `LOGO` partition layouts.
- The charger, fastboot, lowpower, battery and other non-logo blocks.
- The 24 empty placeholder records and their intended use.
- Integrity of `aboot` or how `aboot` selects a block for a given panel/state.
- Channel order on other dumps. BGR is only the confirmed default for the
  reference dump; use `--channel-order` if needed.
- The exact meaning of unknown header fields, especially +0x2c and the bytes
  at +0x08..+0x1f.

## Flashing warning

Writing a partition is done by the user, at the user's own risk. This tool
never writes to a block device, never calls `adb` or `fastboot`, and never
flashes anything.

Before flashing:

1. Make a backup of the original partition and verify it.
2. Verify the partition size and compare it with the image size.
3. After writing, verify the hash of the written data.
4. Have a known-good fastboot/`dd` rollback path ready.

The commands below are reference only, not part of the tool. Partition paths
and names differ between devices. Check them before use.

```sh
# Backup from a root shell or recovery
dd if=/dev/block/bootdevice/by-name/LOGO of=/sdcard/LOGO_backup.img bs=4096
sha256sum /sdcard/LOGO_backup.img

# Reference write with dd (only if you know the exact device path)
dd if=/sdcard/LOGO_new.img of=/dev/block/bootdevice/by-name/LOGO bs=4096
sync

# Reference fastboot flash and rollback (partition name may differ)
fastboot flash LOGO LOGO_new.img
fastboot flash LOGO LOGO_backup.img
```

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The tests build a small synthetic image themselves. The real phone dump is not
included. `*.img` is ignored by Git on purpose: partition dumps are third-party
data and should not be committed.

## License

MIT. See [LICENSE](LICENSE).

---

# logotool (по-русски)

`logotool.py` — один файл для просмотра, декодирования и пересборки образов
раздела `LOGO` (`SPLASH!!` RLE) на OnePlus/OPPO. Используются только
стандартная библиотека Python и Pillow. Инструмент сам ничего не прошивает.

Описание формата: [FORMAT.md](FORMAT.md). Формат подтверждён на одном дампе
OnePlus 5; честный список проверенного и непроверенного — ниже.

## Требования

- Python 3.9 или новее
- Pillow (`python3 -m pip install Pillow`)

## Быстрый старт

```sh
# Таблица: индекс, смещение, размер, слот, запас, имя, группы дублей
python3 logotool.py info LOGO.img

# Декодирование в RGB PNG и out/manifest.json
python3 logotool.py decode LOGO.img out

# Правим нужные PNG. Размеры не менять, сохранять как RGB.

# Пересборка. Меняются только изменённые блоки, остальные байты копируются.
python3 logotool.py encode LOGO.img out --out LOGO_new.img
```

`decode` отказывается писать в непустую папку без `--force`. `encode` никогда
не пишет поверх входного файла и не перезаписывает существующий выходной файл
без `--force`. Флаг `--dry-run` выполняет все проверки и сборку, но не пишет
файл.

### Выбор блоков

```sh
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only 0
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only fhd_oppo
python3 logotool.py encode LOGO.img out --out LOGO_new.img --only 0,2 --dry-run
```

`--only` принимает индекс, полное или короткое имя. Если у блока есть дубли и
они должны остаться одинаковыми, добавьте `--apply-to-duplicates`.

### Если блок перестал влезать в слот

Редакторы вроде mtPaint квантуют цвета и дают шум вроде `(252,253,253)` вместо
белого. Из-за этого RLE раздувается примерно вдвое и блок не влезает.

```sh
# Пиксели светлее N становятся белыми, остальные — чёрными.
python3 logotool.py encode LOGO.img out --out LOGO_new.img \
    --snap-threshold 200 --report
```

`--report` выводится только при переполнении слота: самые дорогие полосы по
40 строк и топ цветов в них.

### RGBA и палитра

`encode` принимает строго RGB PNG. RGBA и палитру он молча не конвертирует.
Для RGBA (а также LA/PA) есть `--flatten`: изображение накладывается на чёрный
фон.

```sh
python3 logotool.py encode LOGO.img out --out LOGO_new.img --flatten
```

PNG с палитрой нужно пересохранить как RGB. Если в другом дампе другой порядок
каналов, используйте `--channel-order bgr|rgb` в `decode` (или `encode`).

## Правила рисования под бюджет слота

- Плоские заливки и жёсткие края.
- Без градиентов, сглаживания, размытия и дизеринга.
- Фон строго `#000000` (`0,0,0`).
- Размеры PNG должны точно совпадать с заменяемым блоком.
- Сохранять как RGB, не RGBA и не палитру.
- Чем меньше цветов и чем длиннее одинаковые участки, тем лучше сжатие.
- Если редактор добавил шум около белого/чёрного, поможет `--snap-threshold N`.
  Это бинарный порог: изображение станет чёрно-белым, поэтому не используйте
  его там, где нужны серые или сглаженные края.
- `--report` показывает самые дорогие по сжатию строки.

## Проверено / не проверено

### Проверено

- Формат и RLE проверены на одном дампе OnePlus 5:
  - 84 записи таблицы, из них 60 реальных блоков и 24 пустые записи;
  - присутствуют изображения панели 1080x1920;
  - распаковка и повторное сжатие совпадают с дампом;
  - decode + encode без изменений даёт побайтово тот же образ.
- Блок 0 (основной логотип) прошит через `dd`, логотип на устройстве
  изменился.

### Не проверено

- Другие устройства и другие раскладки раздела `LOGO`.
- Блоки charger, fastboot, lowpower, battery и другие не-логотипные блоки.
- 24 пустые записи-заглушки и их назначение.
- Целостность `aboot` и то, как `aboot` выбирает блок для панели/состояния.
- Порядок каналов на других дампах. BGR — подтверждённый вариант только для
  эталонного дампа; при необходимости используйте `--channel-order`.
- Точный смысл неизвестных полей заголовка, особенно +0x2c и байтов
  +0x08..+0x1f.

## Предупреждение о прошивке

Запись в раздел делает пользователь и на свой риск. Инструмент не пишет в
блочные устройства, не вызывает `adb`/`fastboot` и ничего не прошивает.

Перед прошивкой:

1. Сделайте бэкап исходного раздела и проверьте его.
2. Сверьте размер раздела с размером образа.
3. После записи сверьте хэш записанных данных.
4. Заранее подготовьте проверенный путь отката через fastboot/`dd`.

Команды ниже — только справка, не код инструмента. Пути и имена разделов
зависят от устройства; проверяйте их.

```sh
# Бэкап из root-шелла или recovery
dd if=/dev/block/bootdevice/by-name/LOGO of=/sdcard/LOGO_backup.img bs=4096
sha256sum /sdcard/LOGO_backup.img

# Справочная запись через dd (только если точно знаете путь устройства)
dd if=/sdcard/LOGO_new.img of=/dev/block/bootdevice/by-name/LOGO bs=4096
sync

# Справочная прошивка и откат через fastboot (имя раздела может отличаться)
fastboot flash LOGO LOGO_new.img
fastboot flash LOGO LOGO_backup.img
```

## Тесты

```sh
python3 -m unittest discover -s tests -v
```

Тесты сами собирают небольшой синтетический образ. Реальный дамп телефона в
репозиторий не входит. `*.img` специально добавлен в `.gitignore`: дампы
разделов — чужие данные, их не нужно коммитить.

## Лицензия

MIT. См. [LICENSE](LICENSE).
