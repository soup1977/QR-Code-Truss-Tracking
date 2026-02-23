import os
import re
import shutil
import json
import time
import dropbox
from dropbox.exceptions import ApiError
from dropbox.files import WriteMode, FolderMetadata
from dropbox.sharing import SharedLinkSettings, RequestedVisibility
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import queue
import qrcode
from qrcode.image.svg import SvgPathImage
from PIL import Image, ImageTk
import webbrowser
import sys

# Backend (from final_fixed)
QR_SUFFIX = '_QR.svg'
TOKEN_FILE = 'token_dropbox.json'
_DROPBOX_ROOT_CANDIDATES = [
    '/Cloud Manager Uploads',
    '/cloud manager uploads',
    '/Cloud manager Uploads',
    '/Cloud Manager uploads',
]

# Status labels
STATUS_UPLOADED = 'Uploaded'
STATUS_DROPBOX = 'Dropbox'
STATUS_LOCAL = 'Local'
STATUS_INCOMPLETE = 'Incomplete'
STATUS_PENDING = 'Pending'
STATUS_ERROR = 'Error'
STATUS_CSV_ONLY = "CSV only"

TAG_MAPPING = {
    STATUS_UPLOADED.lower(): 'uploaded',
    STATUS_DROPBOX.lower(): 'dropbox',
    STATUS_LOCAL.lower(): 'local',
    STATUS_INCOMPLETE.lower(): 'incomplete',
    STATUS_PENDING.lower(): 'pending',
    STATUS_ERROR.lower(): 'error',
    'uploading': 'uploading',
    'local_only': 'local_only',
}

# Dropbox live helpers
def resolve_dropbox_root(dbx):
    for cand in _DROPBOX_ROOT_CANDIDATES:
        try:
            md = dbx.files_get_metadata(cand)
            if isinstance(md, FolderMetadata):
                return cand
        except Exception:
            continue
    return '/Cloud Manager Uploads'

def list_dropbox_jobs(dbx):
    jobs = {}
    root = resolve_dropbox_root(dbx)
    try:
        res = dbx.files_list_folder(root)
    except Exception:
        return jobs
    def process(entries):
        for ent in entries:
            if isinstance(ent, FolderMetadata):
                name = ent.name.strip()
                if re.fullmatch(r'\d{7}', name):
                    jobs[name.upper()] = f'{root}/{name}'
    process(res.entries)
    while res.has_more:
        res = dbx.files_list_folder_continue(res.cursor)
        process(res.entries)
    return jobs

# TTL cache
_last_dropbox_jobs = None
_last_dropbox_jobs_time = 0
_DROPBOX_TTL = 5

def list_dropbox_jobs_cached(dbx):
    global _last_dropbox_jobs, _last_dropbox_jobs_time
    if _last_dropbox_jobs and (time.time() - _last_dropbox_jobs_time) < _DROPBOX_TTL:
        return _last_dropbox_jobs
    _last_dropbox_jobs = list_dropbox_jobs(dbx)
    _last_dropbox_jobs_time = time.time()
    return _last_dropbox_jobs

def find_dropbox_folder(job_id, dbx, cache=None):
    key = job_id.strip().upper()
    if cache is None:
        cache = list_dropbox_jobs_cached(dbx)
    if key in cache:
        return cache[key]
    for k, path in cache.items():
        if k.lower() == job_id.lower():
            return path
    raise FileNotFoundError(f'Dropbox folder for job {job_id} not found under Dropbox uploads root.')

def ensure_dropbox_folder(dbx, job_id):
    root = resolve_dropbox_root(dbx)
    folder_path = f'{root}/{job_id.strip()}'
    try:
        md = dbx.files_get_metadata(folder_path)
        if isinstance(md, FolderMetadata):
            return folder_path
    except Exception:
        try:
            dbx.files_create_folder_v2(folder_path)
            return folder_path
        except Exception as e2:
            raise RuntimeError(f'Cannot create Dropbox folder for {job_id}: {e2}')
    return folder_path

def upload_file_to_dropbox(dbx, local_path, dropbox_folder):
    dest = f'{dropbox_folder}/{os.path.basename(local_path)}'
    with open(local_path, 'rb') as f:
        dbx.files_upload(f.read(), dest, mode=WriteMode('overwrite'))
    return dest

def create_shared_link(dbx, dropbox_folder):
    try:
        links = dbx.sharing_list_shared_links(path=dropbox_folder, direct_only=True).links
        if links:
            return links[0].url
    except Exception:
        pass
    settings = SharedLinkSettings(requested_visibility=RequestedVisibility.public)
    link = dbx.sharing_create_shared_link_with_settings(dropbox_folder, settings)
    return link.url

def classify_local_job(job_id, target_folder):
    job_path = os.path.join(target_folder, job_id)
    if not os.path.isdir(job_path):
        return (STATUS_ERROR, TAG_MAPPING.get(STATUS_ERROR.lower()))

    files = os.listdir(job_path)
    jlow = job_id.lower()

    has_csv  = any(f.lower().endswith('.csv')  and f.lower().startswith(jlow) for f in files)
    has_pdf  = any(f.lower().endswith('.pdf')  and f.lower().startswith(jlow) for f in files)
    has_other = any(f.lower().endswith(('.pdf', '.txt', '.json')) and not f.lower().endswith('.csv') for f in files)

    # CSV + (PDF/TXT/JSON) => Local
    if has_csv and has_other:
        return (STATUS_LOCAL, TAG_MAPPING.get(STATUS_LOCAL.lower()))
    
    # Solo CSV => CSV Only (nuevo estado)
    if has_csv and not has_pdf and not has_other:
        return ('CSV Only', TAG_MAPPING.get(STATUS_PENDING.lower()))

    # Solo PDF => Incomplete
    if has_pdf and not has_csv:
        return (STATUS_INCOMPLETE, TAG_MAPPING.get(STATUS_INCOMPLETE.lower()))

    # Nada útil => Error
    return (STATUS_ERROR, TAG_MAPPING.get(STATUS_ERROR.lower()))

# ----------------------------
# Configuración de persistencia
# ----------------------------
CONFIG_FILE = 'config.json'

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            data = json.load(f)
        return data.get('folder_to_watch', ''), data.get('target_folder', '')
    return '', ''

def save_config():
    with open(CONFIG_FILE, 'w') as f:
        json.dump({
            'folder_to_watch': folder_to_watch,
            'target_folder':   target_folder
        }, f, indent=2)

# ----------------------------
# Funciones de integración
# ----------------------------
APP_KEY = "gtkf02qx1t7fka7"
APP_SECRET = "0gmdo63eb5t5dwn"

def authenticate_dropbox(auth_code=None):
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            data = json.load(f)
        return dropbox.Dropbox(
            oauth2_access_token=data['access_token'],
            oauth2_refresh_token=data.get('refresh_token'),
            app_key=APP_KEY,
            app_secret=APP_SECRET
        )
    if not auth_code:
        raise ValueError('An authorization code is required for initial authentication.')
    flow = dropbox.DropboxOAuth2FlowNoRedirect(APP_KEY, APP_SECRET, token_access_type='offline')
    oauth = flow.finish(auth_code)
    data = {'access_token': oauth.access_token, 'refresh_token': getattr(oauth, 'refresh_token', None)}
    with open(TOKEN_FILE, 'w') as f:
        json.dump(data, f)
    return dropbox.Dropbox(
        oauth2_access_token=data['access_token'],
        oauth2_refresh_token=data.get('refresh_token'),
        app_key=APP_KEY,
        app_secret=APP_SECRET
    )

def show_auth_window():
    auth_window = tk.Toplevel(root)
    auth_window.title("Dropbox Authentication")
    auth_window.geometry("300x300")
    auth_window.resizable(False, False)
    ttk.Label(auth_window, text="Authentication required", font=('Helvetica', 12, 'bold')).pack(pady=10)
    ttk.Label(auth_window,
              text="To use Cloud Manager, you must authorize the app with your Dropbox account.",
              wraplength=280, justify='center').pack(pady=5)
    center_frame = ttk.Frame(auth_window)
    center_frame.pack(expand=True, pady=10)
    auth_url = f"https://www.dropbox.com/oauth2/authorize?response_type=code&client_id={APP_KEY}&token_access_type=offline"
    auth_button = ttk.Button(center_frame,
                           text="Open Dropbox to authorize",
                           command=lambda: webbrowser.open(auth_url))
    auth_button.pack(pady=15, ipadx=10, ipady=5)
    ttk.Label(center_frame, text="After authorizing, paste the code here:").pack(pady=5)
    code_entry = ttk.Entry(center_frame, width=40)
    code_entry.pack(pady=5)
    def on_submit():
        code = code_entry.get().strip()
        if not code:
            messagebox.showerror("Error", "Please enter the authorization code")
            return
        try:
            global dbx
            dbx = authenticate_dropbox(code)
            auth_window.destroy()
            messagebox.showinfo("Éxito", "Authentication completed successfully")
            root.deiconify()
        except Exception as e:
            messagebox.showerror("Error", f"Authentication failed: {str(e)}")
    submit_button = ttk.Button(center_frame, text="Continue", command=on_submit)
    submit_button.pack(pady=15, ipadx=20, ipady=5)
    auth_window.protocol("WM_DELETE_WINDOW", lambda: root.destroy())
    auth_window.grab_set()

# ----------------------------
# Funciones de la aplicación
# ----------------------------
folder_to_watch, target_folder = load_config()

def find_source_folder(job_id):
    for root_dir, dirs, _ in os.walk(folder_to_watch):
        if '_Final Jobsite Package QR' in dirs:
            path = os.path.join(root_dir, '_Final Jobsite Package QR')
            if any(f.startswith(job_id) for f in os.listdir(path)):
                return path
    return None

def copy_to_local(job_id):
    src = find_source_folder(job_id)
    if not src:
        raise FileNotFoundError(f'No source for {job_id}')
    dst = os.path.join(target_folder, job_id)
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(src):
        if f.lower().endswith('.pdf') and f.startswith(job_id) and not re.match(rf"{re.escape(job_id)}_JOB_", f, flags=re.IGNORECASE):
            shutil.copy2(os.path.join(src, f), dst)
        if re.fullmatch(rf"^{job_id}\.csv$", f, flags=re.IGNORECASE):
            shutil.copy2(os.path.join(src, f), dst)
    return dst

show_uploaded = True

def scan_jobs(force: bool = False):
    global show_uploaded
    if not folder_to_watch or not target_folder:
        messagebox.showerror('Error', 'Select both folders first')
        return
    try:
        btn_validate.config(state='disabled')
    except:
        pass

    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    new_snapshot = {}

    # 1. Colección de jobs locales
    local_jobs = set()
    if os.path.isdir(target_folder):
        for d in os.listdir(target_folder):
            if re.fullmatch(r'\d{7}', d.strip()):
                local_jobs.add(d.strip().upper())

    # 2. Colección de jobs pendientes (desde carpeta watch)
    pending_jobs = set()
    if os.path.isdir(folder_to_watch):
        for root_dir, dirs, _ in os.walk(folder_to_watch):
            if '_Final Jobsite Package QR' in dirs:
                qr_dir = os.path.join(root_dir, '_Final Jobsite Package QR')
                for f in os.listdir(qr_dir):
                    m = re.match(r'^(\d{7})', f.strip())
                    if m and m.group(1).upper() not in local_jobs:
                        pending_jobs.add(m.group(1).upper())

    # 3. Colección de jobs en Dropbox (live)
    dropbox_jobs_map = list_dropbox_jobs_cached(dbx)
    dropbox_jobs = set(dropbox_jobs_map.keys())

    # 4. Estado Uploaded (ambos)
    for job_id in sorted(dropbox_jobs & local_jobs):
        new_snapshot[job_id] = {
            'status': STATUS_UPLOADED,
            'progress': '100%',
            'timestamp': now
        }

    # 5. Estado Dropbox (solo nube)
    for job_id in sorted(dropbox_jobs - local_jobs):
        new_snapshot[job_id] = {
            'status': STATUS_DROPBOX,
            'progress': '100%',
            'timestamp': now
        }

    # 6. Estado Local / CSV Only / Incomplete / Error (solo local)
    for job_id in sorted(local_jobs - dropbox_jobs):
        status_text, _ = classify_local_job(job_id, target_folder)
        progress = (
            '50%' if status_text == STATUS_LOCAL else
            '25%' if status_text == 'CSV Only' else
            '25%' if status_text == STATUS_INCOMPLETE else
            '0%'
        )
        new_snapshot[job_id] = {
            'status': status_text,
            'progress': progress,
            'timestamp': now
        }

    # 7. Estado Pending o Incomplete (solo carpeta watch, excluyendo Dropbox)
    for job_id in sorted(pending_jobs - dropbox_jobs):
        src = find_source_folder(job_id)
        if not src:
            # no hay carpeta QR válida → pending normal
            status = STATUS_PENDING
        else:
            files = os.listdir(src)
            # CSV existe?
            has_csv = any(f.lower().endswith('.csv') and f.startswith(job_id) for f in files)
            # Otros PDFs que NO sean *_JOB_*.pdf
            has_real_pdf = any(
                f.lower().endswith('.pdf')
                and f.startswith(job_id)
                and not re.match(rf"{re.escape(job_id)}_JOB_", f, flags=re.IGNORECASE)
                for f in files
            )
            # Sólo JOB_ → ignorar
            if not has_csv and not has_real_pdf:
                continue
            # Sólo CSV → CSV Only
            if has_csv and not has_real_pdf:
                status = 'CSV Only'
            # Sólo PDF → Incomplete
            elif has_real_pdf and not has_csv:
                status = STATUS_INCOMPLETE
            else:
                # tiene al menos un PDF real y CSV → pending
                status = STATUS_PENDING

        new_snapshot[job_id] = {
            'status': status,
            'progress': '0%',
            'timestamp': now
        }

    # 8. Refrescar TreeView
    tree.delete(*tree.get_children())
    for job_id in sorted(new_snapshot.keys()):
        status = new_snapshot[job_id]['status']
        progress = new_snapshot[job_id]['progress']
        timestamp = new_snapshot[job_id]['timestamp']

        if status == STATUS_UPLOADED and not show_uploaded:
            continue

        if status == STATUS_UPLOADED:
            tag = 'uploaded'
        elif status == STATUS_DROPBOX:
            tag = 'dropbox'
        elif status == 'CSV Only':
            tag = 'pending'  # Usar el mismo color que Pending
        elif status == STATUS_INCOMPLETE:
            tag = 'error'
        elif status == STATUS_LOCAL:
            tag = 'local'
        elif status == STATUS_PENDING:
            tag = 'pending'
        else:
            tag = ''

        tree.insert('', 'end',
                    values=(job_id, status, progress, timestamp),
                    tags=(tag,))

    try:
        btn_validate.config(state='normal')
    except:
        pass

def _upload_jobs_thread(job_ids, dbx, folder_to_watch, target_folder):
    for job_id in job_ids:
        nowf = lambda: datetime.now().strftime('%Y-%m-%d %H:%M')
        item = None
        for i in tree.get_children():
            if tree.item(i, 'values')[0] == job_id:
                item = i
                break
        try:
            if item:
                tree.item(item, values=(job_id,'Copying','0%',nowf()), tags=('local_only',))
                root.update_idletasks()
            folder = copy_to_local(job_id)
            pdfs = sorted([f for f in os.listdir(os.path.join(target_folder, job_id)) if f.lower().endswith('.pdf') and f.startswith(job_id)])
            csvs = sorted([f for f in os.listdir(os.path.join(target_folder, job_id)) if f.lower().endswith('.csv') and f.startswith(job_id)])
            
            dbf = ensure_dropbox_folder(dbx, job_id)
            
            # Subir archivos PDF si existen
            if pdfs:
                for i, pdf in enumerate(pdfs, start=1):
                    pct = int(i/len(pdfs)*100)
                    if item:
                        tree.item(item, values=(job_id,f'Uploading {i}/{len(pdfs)}',f'{pct}%',nowf()), tags=('uploading',))
                        root.update_idletasks()
                    upload_file_to_dropbox(dbx, os.path.join(target_folder, job_id, pdf), dbf)
            
            # Subir archivos CSV si existen
            if csvs:
                for i, csv_file in enumerate(csvs, start=1):
                    if item and not pdfs:  # Solo mostrar progreso si no hay PDFs
                        pct = int(i/len(csvs)*100)
                        tree.item(item, values=(job_id,f'Uploading CSV {i}/{len(csvs)}',f'{pct}%',nowf()), tags=('uploading',))
                        root.update_idletasks()
                    upload_file_to_dropbox(dbx, os.path.join(target_folder, job_id, csv_file), dbf)
            
            # Notificar si la carpeta Dropbox está vacía (solo se creó la carpeta)
            if not pdfs and not csvs:
                messagebox.showinfo(f'Empty Folder Created', f'A Dropbox folder was created for {job_id} but no files were uploaded.')
            
            # Crear QR solo si hay archivos subidos
            if pdfs or csvs:
                link = create_shared_link(dbx, dbf)
                local_folder = os.path.join(target_folder, job_id)
                qr = qrcode.QRCode(box_size=10,border=1); qr.add_data(link); qr.make(fit=True)
                img = qr.make_image(image_factory=SvgPathImage)
                img.save(os.path.join(local_folder, f"{job_id}{QR_SUFFIX}"))
            
            if item:
                tree.item(item, values=(job_id,'Uploaded','100%',nowf()), tags=('uploaded',))
                root.update_idletasks()
                
        except Exception as e:
            if item:
                tree.item(item, values=(job_id,'Error','0%',nowf()), tags=('error',))
                root.update_idletasks()
            messagebox.showerror(f'Error {job_id}', str(e))

def _copy_from_dropbox_thread(job_ids, dbx, target_folder):
    for job_id in job_ids:
        nowf = lambda: datetime.now().strftime('%Y-%m-%d %H:%M')
        item = None
        for i in tree.get_children():
            if tree.item(i, 'values')[0] == job_id:
                item = i
                break
        try:
            if item:
                tree.item(item, values=(job_id, 'Copying from Dropbox', '0%', nowf()), tags=('dropbox',))
                root.update_idletasks()
            dropbox_folder = find_dropbox_folder(job_id, dbx, cache=list_dropbox_jobs_cached(dbx))
            local_folder = os.path.join(target_folder, job_id)
            os.makedirs(local_folder, exist_ok=True)
            try:
                res = dbx.files_list_folder(dropbox_folder)
                entries = []
                while True:
                    entries.extend(res.entries)
                    if res.has_more:
                        res = dbx.files_list_folder_continue(res.cursor)
                    else:
                        break
            except ApiError as e:
                raise RuntimeError(f'Error listing Dropbox folder for {job_id}: {e}')
            for ent in entries:
                if not hasattr(ent, 'name'):
                    continue
                name = ent.name
                if name.lower().endswith('.pdf') and name.startswith(job_id) and not re.match(rf"{re.escape(job_id)}_JOB_", name, flags=re.IGNORECASE):
                    local_path = os.path.join(local_folder, name)
                    download_path = f"{dropbox_folder}/{name}"
                    try:
                        _, res_file = dbx.files_download(download_path)
                        with open(local_path, 'wb') as f:
                            f.write(res_file.content)
                    except ApiError as e:
                        print(f'Failed downloading {name} for {job_id}: {e}')
                if re.fullmatch(rf'^{job_id}\.csv$', name, flags=re.IGNORECASE):
                    local_path = os.path.join(local_folder, name)
                    download_path = f"{dropbox_folder}/{name}"
                    try:
                        _, res_file = dbx.files_download(download_path)
                        with open(local_path, 'wb') as f:
                            f.write(res_file.content)
                    except ApiError as e:
                        print(f'Failed downloading CSV {name} for {job_id}: {e}')
            dbf = ensure_dropbox_folder(dbx, job_id)
            link = create_shared_link(dbx, dbf)
            qr = qrcode.QRCode(box_size=10, border=1)
            qr.add_data(link)
            qr.make(fit=True)
            img = qr.make_image(image_factory=SvgPathImage)
            img.save(os.path.join(local_folder, f"{job_id}{QR_SUFFIX}"))
            if item:
                tree.item(item, values=(job_id, 'Uploaded', '100%', nowf()), tags=('uploaded',))
                root.update_idletasks()
        except Exception as e:
            if item:
                tree.item(item, values=(job_id, 'Error', '0%', nowf()), tags=('error',))
                root.update_idletasks()
            messagebox.showerror(f'Error {job_id}', str(e))

def open_job_folder(job_id):
    path = os.path.join(target_folder, job_id)
    if os.path.isdir(path):
        try:
            if os.name == 'nt':
                os.startfile(path)
            elif sys.platform == 'darwin':
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception:
            messagebox.showerror("Error", f"Cannot open folder: {path}")
    else:
        messagebox.showerror("Error", f"Local folder for {job_id} not found.")

def open_job_link(job_id):
    try:
        dropbox_folder = find_dropbox_folder(job_id, dbx, cache=list_dropbox_jobs_cached(dbx))
    except FileNotFoundError:
        messagebox.showerror("Error", f"Job {job_id} has no folder in Dropbox.")
        return
    try:
        link = create_shared_link(dbx, dropbox_folder)
        webbrowser.open(link)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to open shared link: {e}")

def process_selected_jobs():
    sel_items = tree.selection()
    if not sel_items:
        messagebox.showinfo('Upload', 'No job selected')
        return
    uploaded = []
    dropbox_only = []
    to_upload = []
    for item in sel_items:
        job_id = tree.item(item, 'values')[0]
        tags = tree.item(item, 'tags') or ()
        if 'uploaded' in tags:
            uploaded.append(job_id)
        elif 'dropbox' in tags:
            dropbox_only.append(job_id)
        else:
            to_upload.append(job_id)
    final_upload_ids = []
    if uploaded and to_upload:
        ans = messagebox.askyesno(
            'Re-upload jobs?',
            f'The following jobs have already been uploaded: {", ".join(uploaded)}. Would you like to re-upload them along with {", ".join(to_upload)}?'
        )
        if ans:
            final_upload_ids = uploaded + to_upload
        else:
            final_upload_ids = to_upload
    elif uploaded:
        ans = messagebox.askyesno(
            'Re-upload jobs?',
            f'The following jobs have already been uploaded: {", ".join(uploaded)}. Do you want to upload them again?'
        )
        if ans:
            final_upload_ids = uploaded
    else:
        final_upload_ids = to_upload
    if final_upload_ids:
        items_to_select = []
        for job_id in final_upload_ids:
            for item in tree.get_children():
                if tree.item(item, 'values')[0] == job_id:
                    items_to_select.append(item)
                    break
        if items_to_select:
            tree.selection_set(items_to_select)
        threading.Thread(target=_upload_jobs_thread, args=(final_upload_ids, dbx, folder_to_watch, target_folder), daemon=True).start()
    if dropbox_only:
        items_to_select = []
        for job_id in dropbox_only:
            for item in tree.get_children():
                if tree.item(item, 'values')[0] == job_id:
                    items_to_select.append(item)
                    break
        if items_to_select:
            tree.selection_set(items_to_select)
        threading.Thread(target=_copy_from_dropbox_thread, args=(dropbox_only, dbx, target_folder), daemon=True).start()

def sync_all():
    scan_jobs(force=True)

    upload_ids = []      # Pending / Local / Incomplete → subir
    dropbox_ids = []     # Dropbox-only → descargar a local

    for item in tree.get_children():
        vals = tree.item(item, 'values')
        tags = tree.item(item, 'tags') or ()
        if not vals:
            continue
        job_id = vals[0]
        status = vals[1] if len(vals) > 1 else ''

        if 'dropbox' in tags or status == 'Dropbox':
            dropbox_ids.append(job_id)
        elif ('pending' in tags or 'local' in tags or 'incomplete' in tags or status in ('Pending','Local','Incomplete')):
            upload_ids.append(job_id)

    # 3) lanza los hilos correctos
    if upload_ids:
        threading.Thread(
            target=_upload_jobs_thread,
            args=(upload_ids, dbx, folder_to_watch, target_folder),
            daemon=True
        ).start()

    if dropbox_ids:
        threading.Thread(
            target=_copy_from_dropbox_thread,
            args=(dropbox_ids, dbx, target_folder),
            daemon=True
        ).start()

def toggle_uploaded():
    global show_uploaded
    show_uploaded = not show_uploaded
    scan_jobs(force=True)

# ----------------------------
# Interfaz gráfica principal
# ----------------------------
if __name__ == "__main__":
    root = tk.Tk()
    root.title("Tindell’s Connect - Cloud Manager")
    root.geometry("500x600")
    root.resizable(False, False)

    ui_queue = queue.Queue()
    root.after(100, lambda: None)

    try:
        if os.path.exists(TOKEN_FILE):
            dbx = authenticate_dropbox()
        else:
            root.withdraw()
            show_auth_window()
    except Exception as e:
        messagebox.showerror("Error", f"Authentication failed: {str(e)}")
        root.destroy()
        exit()

    try:
        ico = Image.open(os.path.join('icons','Trussty_Connect_Logo.png'))
        root.iconphoto(True,ImageTk.PhotoImage(ico))
    except:
        pass

    style = ttk.Style()
    style.configure('TButton', font=('Helvetica', 10, 'bold'))
    style.configure('Icon.TButton', padding=5)
    style.configure('LargeSquare.TButton', width=12, anchor='center', padding=10, font=('Helvetica',10,'bold'))

    def load_icon(n, size=(45,45)):
        try:
            return ImageTk.PhotoImage(Image.open(os.path.join('icons',f'{n}_icon.png')).resize(size,Image.LANCZOS))
        except:
            return None

    icons = {
        'watch': load_icon('watch'),
        'target': load_icon('target'),
        'validate': load_icon('validate'),
        'upload': load_icon('upload'),
        'sync': load_icon('sync'),
        'search': load_icon('search', size=(24,24)),
        'clean': load_icon('clean', size=(24,24))
    }

    main_f = ttk.Frame(root)
    main_f.pack(fill='both', expand=True)

    cont_f = ttk.Frame(main_f)
    cont_f.pack(fill='both', expand=True, padx=10, pady=10)

    b_f = ttk.Frame(cont_f)
    b_f.pack(fill='x', pady=(0,15))
    bc = ttk.Frame(b_f)
    bc.pack(pady=(10,0))
    for c in range(5):
        bc.grid_columnconfigure(c, weight=1, uniform='btns')

    icons.update({
        'watch_unsel':  load_icon('watch_unselected'),
        'watch_sel':    load_icon('watch_selected'),
        'target_unsel': load_icon('target_unselected'),
        'target_sel':   load_icon('target_selected'),
    })

    def onclick_watch():
        global folder_to_watch
        new_folder = filedialog.askdirectory(title="Select Source Folder")
        if new_folder:
            folder_to_watch = new_folder
            save_config()
            btn_watch.config(image=icons['watch_sel'])
            scan_jobs(force=True)

    def onclick_target():
        global target_folder
        new_folder = filedialog.askdirectory(title="Select Destination Folder")
        if new_folder:
            target_folder = new_folder
            save_config()
            btn_target.config(image=icons['target_sel'])
        scan_jobs(force=True)

    btn_watch = tk.Button(
        bc,
        text='To Watch',
        image=icons['watch_sel'] if folder_to_watch else icons['watch_unsel'],
        compound='top',
        command=onclick_watch,
        bd=0, highlightthickness=0, relief='flat',
        bg=root.cget('bg'), activebackground=root.cget('bg'),
        cursor='hand2',
        font=('Helvetica', 10, 'bold')
    )
    btn_watch.grid(row=0, column=0, padx=(0,20), sticky='nsew')

    btn_target = tk.Button(
        bc,
        text='Target',
        image=icons['target_sel'] if target_folder else icons['target_unsel'],
        compound='top',
        command=onclick_target,
        bd=0, highlightthickness=0, relief='flat',
        bg=root.cget('bg'), activebackground=root.cget('bg'),
        cursor='hand2',
        font=('Helvetica', 10, 'bold')
    )
    btn_target.grid(row=0, column=1, padx=10, sticky='nsew')

    btn_validate = tk.Button(bc, text='Validate', image=icons.get('validate'), compound='top', command=lambda: scan_jobs(force=True),
                             bd=0, highlightthickness=0, relief='flat', bg=root.cget('bg'), activebackground=root.cget('bg'), cursor='hand2', font=('Helvetica',10,'bold'))
    btn_validate.grid(row=0, column=2, padx=10, sticky='nsew')

    btn_upload = tk.Button(bc, text='Upload', image=icons.get('upload'), compound='top', command=process_selected_jobs,
                           bd=0, highlightthickness=0, relief='flat', bg=root.cget('bg'),
                           activebackground=root.cget('bg'), cursor='hand2', font=('Helvetica',10,'bold'))
    btn_upload.grid(row=0, column=3, padx=10, sticky='nsew')

    btn_sync = tk.Button(bc, text='Sync All', image=icons.get('sync'), compound='top',command=lambda: threading.Thread(target=sync_all, daemon=True).start(),
                     bd=0, highlightthickness=0, relief='flat', bg=root.cget('bg'),
                     activebackground=root.cget('bg'), cursor='hand2', font=('Helvetica',10,'bold'))
    btn_sync.grid(row=0, column=4, padx=(20,0), sticky='nsew')

    search_frame = ttk.Frame(cont_f)
    search_frame.pack(fill='x', pady=(0,10))

    search_var = tk.StringVar()
    search_entry = ttk.Entry(search_frame,
                            textvariable=search_var,
                            font=('Helvetica',10))
    search_entry.pack(side='left', fill='x', expand=True, padx=(0,5))
    search_entry.insert(0, 'Search... (use status:)')
    search_entry.config(foreground='grey')

    def on_entry_focus_in(e):
        if search_entry.get() == 'Search... (use status:)':
            search_entry.delete(0, tk.END)
            search_entry.config(foreground='black')

    def on_entry_focus_out(e):
        if not search_entry.get().strip():
            search_entry.insert(0, 'Search... (use status:)')
            search_entry.config(foreground='grey')

    search_entry.bind('<FocusIn>', on_entry_focus_in)
    search_entry.bind('<FocusOut>', on_entry_focus_out)

    search_icon = ttk.Label(search_frame,
                           image=icons['search'],
                           cursor='hand2')
    search_icon.pack(side='left', padx=(0,5))
    search_icon.bind('<Button-1>', lambda e: filter_tree(search_var.get()))

    clear_icon = ttk.Label(search_frame,
                          image=icons['clean'],
                          cursor='hand2')
    clear_icon.pack(side='left')
    clear_icon.bind('<Button-1>', lambda e: (search_var.set(''), scan_jobs()))

    def filter_tree(txt):
        st = txt.lower()
        for i in tree.get_children():
            vals = [str(v).lower() for v in tree.item(i,'values')]
            if st.startswith('status:') and st.replace('status:','').strip() in vals[1]:
                tree.reattach(i,'','end')
            elif not st.startswith('status:') and any(st in v for v in vals):
                tree.reattach(i,'','end')
            else:
                tree.detach(i)

    fr = ttk.Frame(cont_f)
    fr.pack(fill='both', expand=True, pady=(0,0))

    tree = ttk.Treeview(fr, columns=('ID','Status','Progress','Timestamp'), show='headings', selectmode='extended')
    for c in ('ID','Status','Progress','Timestamp'):
        tree.heading(c, text=c, anchor='center')
        tree.column(c, width=100, anchor='center')
    tree.column('ID', width=75)
    tree.column('Status', width=120)
    tree.column('Timestamp', width=175)
    tree.bind('<MouseWheel>', lambda e: tree.yview_scroll(int(-1*(e.delta/120)), 'units'))
    tree.pack(fill='both', expand=True)
    tree.tag_configure('pending', background='#ffcdd2')
    tree.tag_configure('local', background='#fff9c4')
    tree.tag_configure('incomplete', background='#ffe8b0')
    tree.tag_configure('uploaded', background='#c8e6c9')
    tree.tag_configure('uploading', background='#bbdefb')
    tree.tag_configure('error', background='lightcoral')
    tree.tag_configure('dropbox', background='#bbdefb')

    context_menu = tk.Menu(root, tearoff=0)
    hide_submenu = tk.Menu(context_menu, tearoff=0)  # Submenú para opciones de ocultar

    # Diccionario para realizar un seguimiento de los elementos ocultos por estado
    hidden_items = {
        'Pending': [],
        'Dropbox': [],
        'Error': [],
        'Local': [],
        'Incomplete': [],
        'Uploaded': [],
        'Complete': []
    }

    def open_job_folder_context(job_id):
        path = os.path.join(target_folder, job_id)
        if os.path.isdir(path):
            try:
                if os.name == 'nt':
                    os.startfile(path)
                elif sys.platform == 'darwin':
                    os.system(f'open "{path}"')
                else:
                    os.system(f'xdg-open "{path}"')
            except Exception:
                messagebox.showerror("Error", f"Unable to open the folder: {path}")
        else:
            messagebox.showerror("Error", f"Local folder for {job_id} not found.")

    def open_job_link_context(job_id):
        try:
            dropbox_folder = find_dropbox_folder(job_id, dbx, cache=list_dropbox_jobs_cached(dbx))
        except FileNotFoundError:
            messagebox.showerror("Error", f"Job {job_id} No Dropbox folder found (it may not have been uploaded).")
            return
        try:
            link = create_shared_link(dbx, dropbox_folder)
            webbrowser.open(link)
        except Exception as e:
            messagebox.showerror("Error", f"Unable to retrieve or open the shared link: {e}")

    def is_job_marked_uploaded(job_id):
        for item in tree.get_children():
            if tree.item(item, "values")[0] == job_id:
                return "uploaded" in tree.item(item, "tags")
        return False

    def upload_job(job_id):
        for item in tree.get_children():
            if tree.item(item, "values")[0] == job_id:
                tree.selection_set(item)
                break
        tags = ()
        for item in tree.get_children():
            if tree.item(item, "values")[0] == job_id:
                tags = tree.item(item, "tags") or ()
                break
        if 'uploaded' in tags:
            ok = messagebox.askyesno("Reupload job?", f"Job {job_id} It's already uploaded. Do you want to upload it again?")
            if not ok:
                return
            threading.Thread(target=_upload_jobs_thread, args=([job_id], dbx, folder_to_watch, target_folder), daemon=True).start()
        elif 'dropbox' in tags:
            threading.Thread(target=_copy_from_dropbox_thread, args=([job_id], dbx, target_folder), daemon=True).start()
        else:
            threading.Thread(target=_upload_jobs_thread, args=([job_id], dbx, folder_to_watch, target_folder), daemon=True).start()
    
    def hide_status(status_type):
        """Hide jobs with the specified status"""
        # Guardar los elementos que se van a ocultar
        hidden_items[status_type] = []
        for item in tree.get_children():
            values = tree.item(item, 'values')
            if len(values) > 1 and values[1] == status_type:
                hidden_items[status_type].append(item)
                tree.detach(item)
    
    def show_hidden_items():
        """Show all hidden items without validation"""
        for status_type, items in hidden_items.items():
            for item in items:
                # Verificar si el item todavía existe antes de reattach
                if item in tree.get_children():
                    tree.reattach(item, '', 'end')
            hidden_items[status_type] = []  # Limpiar la lista de ocultos
        
        # Forzar a mostrar todos los elementos, incluyendo los Uploaded
        global show_uploaded
        show_uploaded = True
        
        # Actualizar la interfaz
        scan_jobs(force=True)
    
    def show_all():
        """Show all jobs"""
        for item in tree.get_children():
            tree.reattach(item, '', 'end')
        # Limpiar todos los elementos ocultos
        for status_type in hidden_items:
            hidden_items[status_type] = []
        
        # Forzar a mostrar todos los elementos, incluyendo los Uploaded
        global show_uploaded
        show_uploaded = True
        
        # Actualizar la interfaz
        scan_jobs(force=True)

    def revalidate_job(job_id):
        """Revalida un job específico y actualiza su estado en la tabla"""
        try:
            print(f"DEBUG: Revalidando job {job_id}")
            
            # Buscar el item en el treeview
            item = None
            for i in tree.get_children():
                if tree.item(i, 'values')[0] == job_id:
                    item = i
                    break
            
            if not item:
                messagebox.showinfo("Info", f"Job {job_id} no encontrado en la lista")
                return
            
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            
            # Forzar actualización de caché de Dropbox
            global _last_dropbox_jobs, _last_dropbox_jobs_time
            _last_dropbox_jobs = None
            _last_dropbox_jobs_time = 0
            
            # Verificar estados
            dropbox_jobs = list_dropbox_jobs_cached(dbx)
            dropbox_exists = job_id in dropbox_jobs
            local_exists = os.path.isdir(os.path.join(target_folder, job_id))
            
            print(f"DEBUG: Dropbox exists: {dropbox_exists}, Local exists: {local_exists}")
            
            # Determinar nuevo estado
            if local_exists and dropbox_exists:
                new_status = STATUS_UPLOADED
                progress = '100%'
                tag = 'uploaded'
            elif dropbox_exists and not local_exists:
                new_status = STATUS_DROPBOX
                progress = '100%'
                tag = 'dropbox'
            elif local_exists and not dropbox_exists:
                status_text, _ = classify_local_job(job_id, target_folder)
                new_status = status_text
                progress = (
                    '50%' if status_text == STATUS_LOCAL else
                    '25%' if status_text == 'CSV Only' else
                    '25%' if status_text == STATUS_INCOMPLETE else
                    '0%'
                )
                tag = (
                    'local' if status_text == STATUS_LOCAL else
                    'pending' if status_text == 'CSV Only' else
                    'error' if status_text == STATUS_INCOMPLETE else
                    'error'
                )
            else:
                # Verificar si está en carpeta watch
                src = find_source_folder(job_id)
                if src:
                    files = os.listdir(src)
                    has_csv = any(f.lower().endswith('.csv') and f.startswith(job_id) for f in files)
                    has_real_pdf = any(
                        f.lower().endswith('.pdf')
                        and f.startswith(job_id)
                        and not re.match(rf"{re.escape(job_id)}_JOB_", f, flags=re.IGNORECASE)
                        for f in files
                    )
                    
                    if has_csv and not has_real_pdf:
                        new_status = 'CSV Only'
                    elif has_real_pdf and not has_csv:
                        new_status = STATUS_INCOMPLETE
                    else:
                        new_status = STATUS_PENDING
                else:
                    new_status = STATUS_PENDING
                    
                progress = '0%'
                tag = 'pending'
            
            # Actualizar el treeview
            tree.item(item, values=(job_id, new_status, progress, now), tags=(tag,))
            
            messagebox.showinfo("Revalidación completada", 
                            f"Job {job_id} revalidado:\nEstado: {new_status}\nProgreso: {progress}")
            
        except Exception as e:
            messagebox.showerror("Error", f"Error al revalidar job {job_id}: {str(e)}")
            print(f"DEBUG: Error en revalidate_job: {e}")

    def show_context_menu(event):
        item = tree.identify_row(event.y)
        if item and item not in tree.selection():
            tree.selection_set(item)
        
        # Limpiar elementos anteriores del menú
        context_menu.delete(0, tk.END)
        hide_submenu.delete(0, tk.END)
        
        # Agregar opciones de ocultar al submenú
        hide_submenu.add_command(label="Pending", command=lambda: hide_status("Pending"))
        hide_submenu.add_command(label="Dropbox", command=lambda: hide_status("Dropbox"))
        hide_submenu.add_command(label="Error", command=lambda: hide_status("Error"))
        hide_submenu.add_command(label="Local", command=lambda: hide_status("Local"))
        hide_submenu.add_command(label="Incomplete", command=lambda: hide_status("Incomplete"))
        hide_submenu.add_command(label="Uploaded", command=lambda: hide_status("Uploaded"))
        hide_submenu.add_command(label="Complete", command=lambda: hide_status("Complete"))
        hide_submenu.add_separator()
        hide_submenu.add_command(label="Show All", command=show_all)
        hide_submenu.add_command(label="Show Hidden Items", command=show_hidden_items)
        
        # Agregar elementos principales al menú
        context_menu.add_cascade(label="Hide", menu=hide_submenu)
        
        if item:
            job_id = tree.item(item, 'values')[0]
            tags = tree.item(item, 'tags') or ()
            
            context_menu.add_command(label="Revalidate Job", command=lambda jid=job_id: revalidate_job(jid))
            context_menu.add_separator()
            
            context_menu.add_command(label="Open Job Folder", command=lambda jid=job_id: open_job_folder_context(jid))
            context_menu.add_command(label="Open Job Link", command=lambda jid=job_id: open_job_link_context(jid))
            if 'dropbox' in tags:
                context_menu.add_command(label="Download to Local", command=lambda jid=job_id: upload_job(jid))
            elif 'uploaded' in tags:
                context_menu.add_command(label="Reupload Job", command=lambda jid=job_id: upload_job(jid))
            else:
                context_menu.add_command(label="Upload / Sync Job", command=lambda jid=job_id: upload_job(jid))
            context_menu.add_command(
                label="Copy Job ID",
                command=lambda jid=job_id: (root.clipboard_clear(), root.clipboard_append(jid))
            )
        else:
            context_menu.add_separator()
            context_menu.add_command(label="Refresh", command=scan_jobs)
            context_menu.add_command(label="Clear Selection", command=lambda: tree.selection_remove(tree.selection()))
        try:
            context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            context_menu.grab_release()

    # AGREGAR ESTAS LÍNEAS - BINDING DEL MENÚ CONTEXTUAL
    tree.bind("<Button-3>", show_context_menu)  # Windows/Linux
    tree.bind("<Button-2>", show_context_menu)  # macOS

    # Initial scan
    root.after_idle(lambda: scan_jobs(force=True))
    root.mainloop()
