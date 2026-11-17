"""One PDF per isolated process, with a memory and time budget. No OCR."""
import io
import json
import logging
import os
import sys


def limit_resources():
    maximum = 512 * 1024 * 1024
    if os.name == 'nt':
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD), ('min_ws', c.c_size_t), ('max_ws', c.c_size_t), ('active', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(c.Structure):
            _fields_ = [('basic', Basic), ('io', c.c_uint64 * 6), ('process_memory', c.c_size_t), ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.GetCurrentProcess.restype = w.HANDLE
        job = kernel.CreateJobObjectW(None, None)
        limits = Extended(); limits.basic.flags = 0x100 | 0x2
        limits.basic.process_time = 20 * 10000000; limits.process_memory = maximum
        if not job or not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
            raise ValueError('Не удалось запустить ограниченный обработчик PDF.')
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))


def extract(raw):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        raise ValueError('PDF защищён паролем. Загрузите незашифрованную копию.')
    if len(reader.pages) > 500:
        raise ValueError('В PDF более 500 страниц. Разделите документ на части.')
    chunks, size, empty = [], 0, []
    for index, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or '').strip()
        if not text:
            empty.append(index)
            continue
        chunk = f'## [Страница {index}]\n\n{text}'
        size += len(chunk) + 2
        if size > 160000:
            raise ValueError('В PDF больше 160 000 символов текста. Разделите документ на части.')
        chunks.append(chunk)
    if not chunks:
        raise ValueError('В PDF нет доступного текста. Для скана сначала выполните OCR и загрузите копию с текстовым слоем.')
    if empty:
        chunks.append('[Страницы без извлечённого текста: ' + ', '.join(map(str, empty)) + '. Для сканов нужен OCR.]')
    return '\n\n'.join(chunks)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    try:
        limit_resources()
        raw = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError('Максимальный размер файла — 10 МБ')
        result = {'text': extract(raw)}
    except ValueError as exc:
        result = {'error': str(exc)}
    except MemoryError:
        result = {'error': 'PDF требует слишком много памяти. Разделите документ на части.'}
    except Exception:
        result = {'error': 'Не удалось прочитать PDF. Сохраните незашифрованную копию заново.'}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
