import os
import re
import threading
import json
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime
from PIL import Image, ImageTk
import pdfplumber
import qrcode
from reportlab.pdfgen import canvas
from reportlab.lib.units import mm
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF
import tempfile
import uuid
import traceback

# ---------------------------
#  Persistencia de carpeta
# ---------------------------
CONFIG_FILE = 'config.json'
CACHE_FILE = 'cache.json'

def save_config(folder_path):
    """Guarda la ruta seleccionada en config.json."""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump({'folder_path': folder_path}, f, indent=2)

def load_config():
    """Carga la ruta guardada o '' si no existe o si el JSON está vacío/dañado."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get('folder_path', '')
        except (json.JSONDecodeError, ValueError):
            # archivo vacío o JSON malformado
            return ''
    return ''

def save_cache(cache: dict):
    """Guarda en disco el cache de validación."""
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, indent=2)

def load_cache() -> dict:
    """Devuelve el cache en memoria, o {} si no existe o está corrupto."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}

# --------------------
#     Main app 
# --------------------
class StickerManager:
    def calculate_sticker_count(self, qty, ply):
        """
        Si ply == 1: cuenta solo qty.
        Si ply != 1: cuenta qty * ply.
        """
        return qty if ply == 1 else qty * ply
    
    _PDF_CACHE = {}
    _PDF_CACHE_TIME = {}
    _FILE_MTIME_CACHE = {}
    
    def __init__(self, root):
        self.root = root
        self.root.withdraw()

        self.hidden_items = {
            'Pending': [],
            'Completed': ['completed_default'],  # ← Completed oculto por defecto
            'Error': []
        }

        # Solo intento show_splash si existe el archivo
        splash_path = os.path.join("icons", "loading.png")
        if os.path.exists(splash_path):
            self.show_splash(splash_path)
        else:
            print(f"[Warning] Splash image no encontrada en {splash_path}, saltando splash.")

        # Inicialización normal
        self.root.title("Tindell's Connect - Sticker Manager")
        self.root.geometry("500x600")
        self.folder_path    = load_config()
        self.sticker_tokens = {}
        self.print_window   = None

        # Ícono de la ventana
        try:
            icon_path  = os.path.join("icons", "Trussty_Connect_Logo.png")
            icon_img   = Image.open(icon_path)
            icon_photo = ImageTk.PhotoImage(icon_img)
            self.root.iconphoto(True, icon_photo)
        except Exception as e:
            print(f"Error loading window icon: {e}")

        self.load_icons()
        self.setup_ui()
        self.rebuild_treeview()

        # Cierro splash (si existía) y muestro ventana
        self.hide_splash()
        self.root.deiconify()

    def show_splash(self, image_path):
        """Muestra la splash screen con la imagen dada."""
        try:
            self.splash = tk.Toplevel(self.root)
            self.splash.overrideredirect(True)
            img   = Image.open(image_path)
            photo = ImageTk.PhotoImage(img)
            lbl   = tk.Label(self.splash, image=photo)
            lbl.image = photo
            lbl.pack()

            # Centrar
            self.splash.update_idletasks()
            w  = self.splash.winfo_width()
            h  = self.splash.winfo_height()
            ws = self.splash.winfo_screenwidth()
            hs = self.splash.winfo_screenheight()
            x  = (ws // 2) - (w // 2)
            y  = (hs // 2) - (h // 2)
            self.splash.geometry(f"{w}x{h}+{x}+{y}")
            self.splash.update()
        except Exception as e:
            print(f"[Warning] No se pudo mostrar splash: {e}")


    def hide_splash(self):
        """Destruye la splash screen si existe."""
        if hasattr(self, "splash"):
            try:
                self.splash.destroy()
            except:
                pass
            delattr(self, "splash")

    def create_custom_button(self, parent, icon_key, text, command):
        """Crea un botón con el mismo estilo que Upload Manager"""
        bg_color = parent.winfo_toplevel().cget('bg')  # (ojo no tocar) Obtiene el color de fondo de la ventana
        btn = tk.Button(
            parent,
            text=text,
            image=self.icons.get(icon_key),
            compound='top',
            command=command,
            bd=0,                           # Sin borde
            highlightthickness=0,           # Sin halo al enfocar
            relief='flat',                  # Estilo plano
            bg=bg_color,                    # Fondo transparente
            activebackground=bg_color,      # Fondo al hacer clic
            cursor='hand2',                 # Cursor de mano
            font=('Helvetica', 10, 'bold')  # Texto en negrita
        )
        return btn
    
    def load_icons(self):
        """Carga todos los íconos necesarios"""
        self.icons = {
            'folder_unsel': self._load_icon("folder_unselected_icon.png"),
            'folder_sel': self._load_icon("folder_selected_icon.png"),
            'validate': self._load_icon("validate_icon.png"),
            'generate': self._load_icon("generate_icon.png"),
            'generate_all': self._load_icon("generate_all_icon.png"),
            'print': self._load_icon("print_icon.png"),
            'search': self._load_icon("search_icon.png", (24, 24)),
            'clean': self._load_icon("clean_icon.png", (24, 24))
        }

    
    def _load_icon(self, icon_name, size=(45, 45)):
        """Carga un ícono individual"""
        try:
            icon_path = os.path.join("icons", icon_name)
            img = Image.open(icon_path).resize(size, Image.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception as e:
            print(f"Error loading icon {icon_name}: {e}")
            return None

    def setup_ui(self):
        """Configura todos los elementos con los mismos márgenes que Upload Manager"""
        # Frame principal
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(18, 10))  # 10px externos

        # Frame de contenido
        content_frame = ttk.Frame(main_frame)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)  # 5px internos

        # Barra de botones
        self.setup_button_bar(content_frame)  # pady=(0, 15) 

        # Barra de búsqueda
        self.setup_search_bar(content_frame)  # pady=(0, 10)

        # Treeview
        self.setup_treeview(content_frame)

        # Banner
        self.setup_banner(content_frame)
    
    def setup_button_bar(self, parent):
        """Configura la barra de botones con el nuevo estilo"""
        button_frame = ttk.Frame(parent)
        button_frame.pack(fill=tk.X, pady=(0, 15))

        for i in range(5):
            button_frame.columnconfigure(i, weight=1, uniform="btns")

        # Define el botón con ícono según estado inicial
        self.btn_select = self.create_custom_button(
            button_frame,
            'folder_sel' if self.folder_path else 'folder_unsel',
            "Select",
            self.select_folder
        )
        self.btn_select.grid(row=0, column=0, padx=5, sticky="nsew")

        btn_validate = self.create_custom_button(
        button_frame, 'validate', "Validate", self.validate_jobs_optimized  # ← CAMBIAR AQUÍ
        )
        btn_validate.grid(row=0, column=1, padx=5, sticky="nsew")

        btn_generate = self.create_custom_button(
            button_frame, 'generate', "Generate", self.generate_selected
        )
        btn_generate.grid(row=0, column=2, padx=5, sticky="nsew")

        btn_generate_all = self.create_custom_button(
            button_frame, 'generate_all', "Generate All", self.generate_all
        )
        btn_generate_all.grid(row=0, column=3, padx=5, sticky="nsew")

        btn_print = self.create_custom_button(
            button_frame, 'print', "Print", self.print_by_batch
        )
        btn_print.grid(row=0, column=4, padx=5, sticky="nsew")
    
    def setup_search_bar(self, parent):
        """Configura la barra de búsqueda con botones personalizados"""
        search_frame = ttk.Frame(parent)
        search_frame.pack(fill=tk.X, pady=(0, 10))

        # Color de fondo - ttk.Style
        style = ttk.Style()
        bg_color = style.lookup('TFrame', 'background')

        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            search_frame, 
            textvariable=self.search_var,
            font=('Helvetica', 10)
        )
        self.search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        self.search_entry.insert(0, "Search... (use 'status:' for status filter)")
        self.search_entry.config(foreground='grey')

        self.search_entry.bind('<FocusIn>', self.on_entry_click)
        self.search_entry.bind('<FocusOut>', self.on_focusout)

        btn_search = tk.Button(
            search_frame,
            image=self.icons['search'],
            command=self.filter_tree,
            bd=0,                       # Sin borde
            highlightthickness=0,       # Sin halo al enfocar
            relief='flat',              # Estilo plano
            bg=bg_color,                # Fondo transparente
            activebackground=bg_color,  # Fondo al hacer clic
            cursor='hand2'              # Cursor de mano
        )
        btn_search.pack(side=tk.LEFT, padx=(0, 5))

        # Botón limpiar (personalizado)
        btn_clear = tk.Button(
            search_frame,
            image=self.icons['clean'],
            command=self.clear_search,
            bd=0,                        # Sin borde
            highlightthickness=0,        # Sin halo al enfocar
            relief='flat',               # Estilo plano
            bg=bg_color,                 # Fondo transparente
            activebackground=bg_color,   # Fondo al hacer clic
            cursor='hand2'               # Cursor de mano
        )
        btn_clear.pack(side=tk.LEFT)
    
    def setup_treeview(self, parent):
        """Configura el Treeview para mostrar los jobs"""
        tree_frame = ttk.Frame(parent)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("ID", "Stickers Qty", "Status", "Timestamp")
        self.tree = ttk.Treeview(
            tree_frame, 
            columns=columns, 
            show="headings",
            selectmode="extended"
        )
        
        # Configurar columnas
        for col in columns:
            self.tree.heading(col, text=col, anchor="center")
            self.tree.column(col, width=100, anchor="center")
        
        # Ajustes específicos
        self.tree.column("ID", width=100)
        self.tree.column("Stickers Qty", width=120)
        self.tree.column("Timestamp", width=150)
        
        # Tags de color
        self.tree.tag_configure('pending',   background='#fff9c4')
        self.tree.tag_configure('completed', background='#c8e6c9')
        self.tree.tag_configure('error',     background='#ffcdd2')
        
        # Layout y scroll
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<MouseWheel>", self.on_mousewheel)  
        self.tree.bind("<Button-4>",   self.on_mousewheel)    
        self.tree.bind("<Button-5>",   self.on_mousewheel)
        
        # Bind para menú contextual
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.context_menu = tk.Menu(self.root, tearoff=0)

        # Si había ruta guardada, carga y reconstruye :-)
        if self.folder_path:
            self.rebuild_treeview()
    
    def setup_banner(self, parent):
        """Configura el banner inferior"""
        banner_frame = tk.Frame(parent, bd=0, highlightthickness=0)   # Frame sin bordes -.-
        banner_frame.pack(fill='x', pady=0, padx=0)                   # Sin márgenes -.-

        # Crea el banner
        try:
            banner_img = Image.open(os.path.join("icons", "Sticker_Manager_Banner.png"))
            banner_img = banner_img.resize((500, 70), Image.LANCZOS)
            banner_photo = ImageTk.PhotoImage(banner_img)
            
            banner_label = tk.Label(banner_frame, 
                                image=banner_photo, 
                                bd=0, 
                                highlightthickness=0)
            banner_label.image = banner_photo
            banner_label.pack(fill='both', expand=True)
            
        except Exception as e:
            print(f"No se pudo cargar el banner: {str(e)}")
            fallback_banner = tk.Canvas(banner_frame, 
                                    height=70, 
                                    bg='#2c3e50', 
                                    highlightthickness=0, 
                                    bd=0,
                                    relief='flat')
            fallback_banner.pack(fill='both', expand=True)
            fallback_banner.create_rectangle(0, 0, 500, 70, fill='#2c3e50', outline='')
            fallback_banner.create_text(250, 35, 
                                    text="Sticker Manager", 
                                    fill='white', 
                                    font=('Helvetica', 20, 'bold'))
    
    def print_by_batch(self):
        """Muestra batches organizados por Job ID con texto más grande y centrado"""
        if self.print_window and tk.Toplevel.winfo_exists(self.print_window):
            self.print_window.lift()
            return

        # Guarda la selección de jobs ANTES de crear la ventana de batches
        selected_jobs = self.tree.selection()
        if not selected_jobs:
            messagebox.showwarning("Warning", "Please select at least one job")
            return

        # Estructura para almacenar: {job_id: {batch_code: count, full_batch: ...}}
        job_batches = {}
        for item in selected_jobs:
            job_id = self.tree.item(item, "values")[0]
            csv_path = os.path.join(self.folder_path, job_id, f"{job_id}.csv")
            if not os.path.exists(csv_path):
                print(f"[DEBUG] CSV not found for job {job_id}")
                continue

            try:
                import pandas as pd
                df = pd.read_csv(csv_path)
                job_batches[job_id] = {}
                for batch_full in df['Batch'].unique():
                    batch_code = str(batch_full)[-2:]
                    if len(batch_code) != 2:
                        continue
                    count = df[df['Batch'] == batch_full]['Qty'].sum()
                    job_batches[job_id][batch_code] = {
                        'count': count,
                        'full_batch': batch_full
                    }
            except Exception as e:
                print(f"[ERROR] Processing CSV for job {job_id}: {e}")

        if not job_batches:
            messagebox.showinfo("Info", "No batches found in selected jobs")
            return

        # ventana de selección
        self.print_window = tk.Toplevel(self.root)
        self.print_window.title("Select Batch(es) to Print")
        self.print_window.geometry("400x400")  # Tamaño reducido
        self.print_window.resizable(False, False)

        def on_close():
            self.print_window.destroy()
            self.print_window = None

        self.print_window.protocol("WM_DELETE_WINDOW", on_close)

        # Estilo y layout
        style = ttk.Style()
        style.configure("Large.Treeview", font=('Helvetica', 10), rowheight=30)
        style.configure("Large.Treeview.Heading", font=('Helvetica', 10, 'bold'))

        main_frame = ttk.Frame(self.print_window)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        main_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(1, weight=1)
        main_frame.columnconfigure(0, weight=1)

        tk.Label(main_frame, text="Select Batches to Open",
                font=("Helvetica", 11, "bold")).grid(row=0, column=0, sticky="ew", pady=(0,5))

        # Treeview de batches
        tree_frame = ttk.Frame(main_frame)
        tree_frame.grid(row=1, column=0, sticky="nsew", pady=(0,5))
        
        columns = ("Job ID", "Batch", "Stickers")
        self.batch_tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                    selectmode="extended", style="Large.Treeview", height=8)
        
        # Configurar columnas
        for col in columns:
            self.batch_tree.heading(col, text=col, anchor="center")
            self.batch_tree.column(col, width=90, anchor="center")
        
        self.batch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # Configurar scroll con rueda del ratón
        self.batch_tree.bind("<MouseWheel>", self.on_batch_tree_scroll)
        self.batch_tree.bind("<Button-4>", self.on_batch_tree_scroll)  # Para Linux (scroll up)
        self.batch_tree.bind("<Button-5>", self.on_batch_tree_scroll)  # Para Linux (scroll down)

        # Llenar batches
        for job_id in sorted(job_batches):
            for batch_code in sorted(job_batches[job_id]):
                self.batch_tree.insert("", "end",
                                    values=(job_id,
                                            batch_code,
                                            job_batches[job_id][batch_code]['count']),
                                    tags=(job_id,))

        colors = ["#e7e7e7", "#ffffff"]
        for i, job_id in enumerate(job_batches):
            self.batch_tree.tag_configure(job_id, background=colors[i % 2])

        # Botones
        btn_frame = ttk.Frame(main_frame)
        btn_frame.grid(row=2, column=0, sticky="ew", pady=(5,0))
        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        btn_frame.columnconfigure(2, weight=1)

        def on_confirm():
            """Abrir batches seleccionados"""
            batch_items = self.batch_tree.selection()
            if not batch_items:
                messagebox.showwarning("Warning", "Please select at least one batch")
                return

            selected_batches = []
            for bi in batch_items:
                jid, bcode = self.batch_tree.item(bi, "values")[:2]
                selected_batches.append(job_batches[jid][bcode]['full_batch'])

            on_close()
            self._open_selected_batches(selected_jobs, selected_batches)

        def on_open_all():
            """Abrir todos los batches"""
            all_batches = []
            for job_id in job_batches:
                for batch_code in job_batches[job_id]:
                    all_batches.append(job_batches[job_id][batch_code]['full_batch'])
            
            on_close()
            self._open_selected_batches(selected_jobs, all_batches)

        ttk.Button(btn_frame, text="PRINT SELECTED", command=on_confirm)\
            .grid(row=0, column=0, padx=2, pady=5, sticky="ew")
        
        ttk.Button(btn_frame, text="PRINT ALL", command=on_open_all)\
            .grid(row=0, column=1, padx=2, pady=5, sticky="ew")
        
        ttk.Button(btn_frame, text="CANCEL", command=on_close)\
            .grid(row=0, column=2, padx=2, pady=5, sticky="ew")

    def get_available_printers(self):
        """Obtiene la lista de impresoras disponibles en el sistema"""
        printers = []
        try:
            if os.name == 'nt':  # Windows
                import win32print
                printers = [printer[2] for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
            elif os.name == 'posix':  # Linux/macOS
                import subprocess
                try:
                    # Para sistemas con lpstat
                    result = subprocess.run(['lpstat', '-a'], capture_output=True, text=True)
                    if result.returncode == 0:
                        printers = [line.split()[0] for line in result.stdout.split('\n') if line.strip()]
                except:
                    # Fallback alternativo
                    try:
                        result = subprocess.run(['lpstat', '-p'], capture_output=True, text=True)
                        if result.returncode == 0:
                            printers = [line.split()[1] for line in result.stdout.split('\n') if 'printer' in line]
                    except:
                        printers = ["Default Printer"]
        except Exception as e:
            print(f"Error getting printers: {e}")
            printers = ["Default Printer"]
        
        return printers if printers else ["Default Printer"]

    def _print_selected_batches(self, selected_jobs, selected_batches, printer_name=None):
        """Filtra y genera PDFs con solo los stickers de los batch seleccionados"""
        for item in selected_jobs:
            job_id = self.tree.item(item, "values")[0]
            pdf_path = os.path.join(self.folder_path, job_id, f"Stickers_{job_id}.pdf")
            
            if not os.path.exists(pdf_path):
                messagebox.showwarning("Missing", f"PDF not found for {job_id}")
                continue

            try:
                from PyPDF2 import PdfReader, PdfWriter
                reader = PdfReader(pdf_path)
                writer = PdfWriter()

                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if any(batch in text for batch in selected_batches):
                        writer.add_page(page)

                if not writer.pages:
                    messagebox.showinfo("No Match", f"No matching stickers found in {job_id}")
                    continue

                # Crear archivo temporal filtrado
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    writer.write(temp_pdf)
                    temp_path = temp_pdf.name

                # Abrir predeterminado
                if os.name == 'nt':  # Windows
                    os.startfile(temp_path)
                elif os.name == 'darwin':  # macOS
                    subprocess.run(['open', temp_path])
                else:  # Linux
                    subprocess.run(['xdg-open', temp_path])
                    
                # El usuario imprimirá manualmente desde el visor PDF
                messagebox.showinfo(
                    "PDF Ready", 
                    f"PDF filtered for job {job_id} is now open.\n"
                    "Please use the print function from your PDF viewer."
                )

            except Exception as e:
                messagebox.showerror("Error", f"Error processing {job_id}:\n{e}")

    def on_batch_tree_scroll(self, event):
        """Maneja el scroll con rueda del ratón en el treeview de batches"""
        if event.num == 4 or event.delta > 0:  # Scroll up (Linux/Windows)
            self.batch_tree.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:  # Scroll down (Linux/Windows)
            self.batch_tree.yview_scroll(1, "units")

    def show_batch_status(self, job_id):
        """Muestra una ventana con el estado de impresión por batch"""
        printed = {}
        for key, data in self.sticker_tokens.items():
            key_parts = key.split("|")
            if len(key_parts) != 3:
                continue
            batch, code, sticker_type = key_parts
            if job_id in batch:
                if batch not in printed:
                    printed[batch] = {"total": 0, "printed": 0}
                printed[batch]["total"] += 1
                if data.get("printed"):
                    printed[batch]["printed"] += 1

        # Crear ventana
        top = tk.Toplevel(self.root)
        top.title(f"Print Status - Job {job_id}")
        top.geometry("200x400")
        top.resizable(False, False)

        tk.Label(top, text=f"Batches for job {job_id}", font=("Helvetica", 12, "bold")).pack(pady=10)

        listbox = tk.Listbox(top, font=("Helvetica", 10))
        for batch, stats in printed.items():
            done = stats["printed"]
            total = stats["total"]
            status = "✅" if done == total else "🕗"
            listbox.insert(tk.END, f"{status} {batch}: {done}/{total} printed")
        listbox.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        tk.Button(top, text="Close", command=top.destroy).pack(pady=5)

    # ==============================================
    # FUNCIONALIDADES PRINCIPALES
    # ==============================================

    def _add_tree_item(self, validation_data):
        import os
        from datetime import datetime

        status_display = {"pending":"Pending","completed":"Completed","error":"Error"}
        job_id   = validation_data["job_id"]
        job_path = os.path.join(self.folder_path, job_id)

        # Total real
        expected    = self.compute_expected_count(job_path)
        qty_display = str(expected) if expected is not None else "Invalid"

        # Si falta archivo, muéstralo (O eso deberia)
        if validation_data["status"] == "error":
            err = validation_data.get("error","")
            if "Faltan archivos:" in err:
                qty_display = err.replace("Faltan archivos:","Missing:").strip()

        self.tree.insert(
            "", "end",
            values=(
                job_id,
                qty_display,
                status_display.get(validation_data["status"],"Unknown"),
                datetime.now().strftime('%Y-%m-%d %H:%M')
            ),
            tags=(validation_data["status"],)
        )
        
    def select_folder(self):
        """Abre diálogo para seleccionar carpeta y persiste la ruta en config.json,
        además limpia el cache si cambia la carpeta."""
        folder = filedialog.askdirectory(title="Select Jobs Folder")
        if folder:
            # Comprueba si cambió la carpeta y elimina el cache anterior
            old_folder = getattr(self, 'folder_path', None)
            if folder != old_folder and os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)

            # Actualiza la carpeta y persiste en config
            self.folder_path = folder
            save_config(folder) 

            # Actualiza el ícono del botón y notifica al usuario
            self.btn_select.config(image=self.icons['folder_sel'])
            messagebox.showinfo(
                "Folder Selected",
                f"Selected folder:\n{folder}"
            )

    def validate_jobs(self):
        """Valida los jobs en la carpeta seleccionada, mostrando solo pendientes o con error"""
        if not self.folder_path:
            messagebox.showerror("Error", "Please select a folder first")
            return

        # Mostrar ventana de progreso
        self.show_progress("Validating jobs...")

        # Ejecutar en hilo separado
        def _task():
            try:
                self.root.after(0, self.tree.delete, *self.tree.get_children())
                job_folders = [f for f in os.listdir(self.folder_path) if re.match(r'^\d{7}$', f)]
                total = len(job_folders)

                for i, job_folder in enumerate(job_folders, 1):
                    job_path = os.path.join(self.folder_path, job_folder)
                    is_valid, validation_data = self.validate_job_folder(job_path)
                    
                    # Solo agregar jobs pendientes o con error
                    if validation_data["status"] in ("pending", "error"):
                        self.update_progress((i / total) * 100, f"Validating {job_folder}")
                        self.root.after(0, self._add_tree_item, validation_data)

                self.hide_progress()

            except Exception as e:
                self.hide_progress()
                messagebox.showerror("Error", f"Validation failed: {str(e)}")

        threading.Thread(target=_task, daemon=True).start()

    def validate_jobs_optimized(self):
        """Versión optimizada de validate_jobs"""
        if not self.folder_path:
            messagebox.showerror("Error", "Please select a folder first")
            return

        self.show_progress("Validating jobs...")
        self.tree.delete(*self.tree.get_children())

        def _task():
            try:
                job_folders = [f for f in os.listdir(self.folder_path) if re.match(r'^\d{7}$', f)]
                total = len(job_folders)
                
                # Validación usando cache
                for i, job_folder in enumerate(job_folders, 1):
                    job_path = os.path.join(self.folder_path, job_folder)
                    is_valid, validation_data = self.validate_job_folder_cached(job_path)
                    
                    # Solo agregar jobs pendientes o con error
                    if validation_data["status"] in ("pending", "error"):
                        self.update_progress((i / total) * 100, f"Validating {job_folder}")
                        self.root.after(0, self._add_tree_item, validation_data)

                self.hide_progress()
                
            except Exception as e:
                self.hide_progress()
                messagebox.showerror("Error", f"Validation failed: {str(e)}")

        threading.Thread(target=_task, daemon=True).start()

    def _validate_jobs_task(self):
        """Valida los jobs, usando cache para acelerar cargas subsecuentes."""
        try:
            
            cache = load_cache()

            # Limpia el Treeview
            self.root.after(0, self.tree.delete, *self.tree.get_children())

            # Listado de carpetas de jobs
            job_folders = [f for f in os.listdir(self.folder_path) if re.match(r'^\d{7}$', f)]
            total = len(job_folders)

            for i, job_folder in enumerate(job_folders, 1):
                # Progreso
                pct = (i / total) * 100

                if job_folder in cache:
                    entry = cache[job_folder]
                    self.update_progress(pct, f"Loading {job_folder} from cache")
                    self.root.after(0, self.tree.insert,
                        "", "end",
                        {
                            "values": (
                                job_folder,
                                entry["qty"],
                                entry["status"].capitalize(),
                                entry["timestamp"]
                            ),
                            "tags": (entry["status"],)
                        }
                    )
                    continue

                job_path = os.path.join(self.folder_path, job_folder)
                is_valid, validation_data = self.validate_job_folder(job_path)

                self.update_progress(pct, f"Validating {job_folder}")
                self.root.after(0, self._add_tree_item, validation_data)

                if is_valid:
                    expected = sum(
                        self.calculate_sticker_count(t["qty"], t["ply"])
                        for t in validation_data["trusses_data"]
                    )
                    qty = str(expected)
                else:
                    # mensaje de error como qty
                    qty = validation_data.get("error", "").replace("Faltan archivos:", "Missing:")

                # Guarda en cache para la próxima ejecución
                cache[job_folder] = {
                    "status":   validation_data["status"],         # "pending" / "completed" / "error"
                    "qty":      qty,
                    "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M')
                }

            save_cache(cache)
            self.hide_progress()

        except Exception as e:
            self.hide_progress()
            messagebox.showerror("Error", f"Validation failed: {str(e)}")

    def validate_job_folder(self, folder_path):
        """Valida la estructura de una carpeta de job con la nueva estructura CSV"""
        if not os.path.isdir(folder_path):
            return False, {"error": "Not a valid folder", "status": "error"}

        folder_name = os.path.basename(folder_path)
        job_id_match = re.match(r'^(\d{7})$', folder_name)
        if not job_id_match:
            return False, {"error": "The ID must be 7 digits", "status": "error"}

        job_id = job_id_match.group(1)
        validation_data = {
            "job_id": job_id,
            "files": {},
            "trusses_data": [],
            "status": "error"
        }

        try:
            # Archivos esenciales - solo CSV y QR son obligatorios ahora
            required_files = [
                ("QR", lambda f: "_qr.svg" in f.lower()),
                ("CSV", lambda f: f.lower().endswith(".csv"))
            ]

            # Buscar archivos
            found_files = {}
            for filename in os.listdir(folder_path):
                for (name, condition) in required_files:
                    if condition(filename):
                        found_files[name] = os.path.join(folder_path, filename)
                        break

            # Preparar datos de validación
            missing_files = []
            files_dict = {}
            for (name, _) in required_files:
                if name in found_files:
                    files_dict[name] = found_files[name]
                else:
                    missing_files.append(name)

            validation_data["files"] = files_dict

            # Verificar archivos faltantes
            if missing_files:
                validation_data["error"] = f"Missing files: {', '.join(missing_files)}"
                return False, validation_data

            # Extraer trusses desde el CSV
            csv_file = files_dict["CSV"]
            trusses_data, error = self.extract_trusses_data(csv_file, job_id)
            if error:
                validation_data["error"] = error
                return False, validation_data
            validation_data["trusses_data"] = trusses_data

            # VERIFICACIÓN SIMPLE: Si existen los stickers = COMPLETED
            stickers_pdf = os.path.join(folder_path, f"Stickers_{job_id}.pdf")
            summary_txt = os.path.join(folder_path, f"{job_id}_Stickers_Summary.txt")
            
            if os.path.exists(stickers_pdf) and os.path.exists(summary_txt):
                validation_data["status"] = "completed"
            else:
                validation_data["status"] = "pending"

            return True, validation_data

        except Exception as e:
            validation_data["error"] = f"Error inesperado: {str(e)}"
            return False, validation_data
        
    def validate_job_folder_cached(self, folder_path):
        """Versión cached de validate_job_folder"""
        cache = load_cache()
        job_id = os.path.basename(folder_path)
        
        if job_id in cache:
            # Verificar si los archivos han cambiado
            if not self._has_files_changed(folder_path, cache[job_id].get("file_mtimes", {})):
                return cache[job_id]["validation_result"]
        
        # Validación normal
        result = self.validate_job_folder(folder_path)
        
        # Actualizar cache
        cache[job_id] = {
            "validation_result": result,
            "file_mtimes": self._get_file_mtimes(folder_path),
            "timestamp": datetime.now().isoformat()
        }
        save_cache(cache)
        
        return result
        
    def compute_expected_count(self, job_folder):
        """
        Suma (Qty × Ply) PARA CADA TrsName del CSV
        """
        import os
        import pandas as pd
        job_id = os.path.basename(job_folder)

        # 1) Find the CSV
        csv_path = None
        for fn in os.listdir(job_folder):
            if fn.lower().endswith('.csv'):
                csv_path = os.path.join(job_folder, fn)
                break

        if not csv_path:
            return None  # No CSV found

        try:
            df = pd.read_csv(csv_path)
            df.columns = df.columns.str.lower()
            
            df = df[df['jobnumber'] == int(job_id)]
            
            if df.empty:
                return None
                
            # Sumar Qty * Ply para todas las filas
            total = 0
            for _, row in df.iterrows():
                qty = int(row['qty'])
                ply = int(row['ply'])
                total += self.calculate_sticker_count(qty, ply)
                
            return total

        except Exception as e:
            print(f"Error reading CSV: {e}")
            return None
    
    def extract_trusses_data(self, csv_path, expected_job_id):
        """Extrae datos de trusses de un archivo CSV con la nueva estructura"""
        trusses = []
        
        try:
            import pandas as pd
            df = pd.read_csv(csv_path)
            
            # Normalizar nombres de columnas (case-insensitive)
            df.columns = df.columns.str.lower()
            
            # Filtrar por jobnumber
            df = df[df['jobnumber'] == int(expected_job_id)]
            
            if df.empty:
                return None, f"No data found for job {expected_job_id} in the CSV"
            
            # Procesar cada fila
            for _, row in df.iterrows():
                truss_info = {
                    "model": f"{row['trsname']} {row['trusstype']}",
                    "qty": int(row['qty']),
                    "ply": int(row['ply']),
                    "type": row['trusstype'],
                    "batch": row['batch'],
                    "client": row['customer'],
                    "project": row['jobname']
                }
                trusses.append(truss_info)
        
        except Exception as e:
            return None, f"Failed to process CSV: {str(e)}"
        
        if not trusses:
            return None, "No valid truss data found in the CSV"
        
        return trusses, None

    def _get_file_mtimes(self, folder_path):
        """Obtiene los tiempos de modificación de los archivos"""
        mtimes = {}
        for filename in os.listdir(folder_path):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                mtimes[filename] = os.path.getmtime(file_path)
        return mtimes

    def _has_files_changed(self, folder_path, cached_mtimes):
        """Verifica si los archivos han cambiado desde la última validación"""
        if not cached_mtimes:
            return True
            
        current_mtimes = self._get_file_mtimes(folder_path)
        
        # Verificar si algún archivo ha cambiado
        for filename, mtime in cached_mtimes.items():
            if filename not in current_mtimes or current_mtimes[filename] != mtime:
                return True
        
        # Verificar si hay archivos nuevos
        for filename in current_mtimes:
            if filename not in cached_mtimes:
                return True
                
        return False

    def generate_selected(self):
        """Genera stickers para todos los jobs seleccionados"""
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "Please select at least one job")
            return
        
        job_ids = [self.tree.item(item, "values")[0] for item in selected]
        self.generate_stickers_threaded(job_ids)

    def generate_stickers_threaded(self, job_ids):
        """Genera stickers en segundo plano para múltiples jobs"""
        if not self.folder_path:
            messagebox.showerror("Error", "Please select a folder first")
            return

        if not job_ids:
            messagebox.showwarning("Warning", "No jobs selected")
            return

        self.show_progress(f"Generating {len(job_ids)} jobs...")

        threading.Thread(
            target=self._generate_stickers_task,
            args=(job_ids,),
            daemon=True
        ).start()

    def _generate_stickers_task(self, job_ids):
        """Tarea que genera stickers uno por uno"""
        success_count = 0
        error_messages = []

        for i, job_id in enumerate(job_ids, 1):
            try:
                job_path = os.path.join(self.folder_path, job_id)
                output_path = os.path.join(job_path, f"Stickers_{job_id}.pdf")

                self.update_progress((i / len(job_ids)) * 100, f"Processing {job_id} ({i}/{len(job_ids)})")

                self.generate_stickers(job_path, output_path)
                success_count += 1
                self.root.after(0, self.update_job_status, job_id, "completed")

            except Exception as e:
                error_messages.append(f"{job_id}: {str(e)}")
                print(f"Error generating {job_id}: {e}")
                traceback.print_exc()
                self.root.after(0, self.update_job_status, job_id, "error")

        self.root.after(0, self._show_generation_result, success_count, len(job_ids), error_messages)
        self.root.after(0, self.hide_progress)
    
    def generate_all(self):
        """Genera stickers para todos los jobs pendientes"""
        pending_jobs = [
            self.tree.item(item, "values")[0] 
            for item in self.tree.get_children() 
            if self.tree.item(item, "values")[2] == "Pending"
        ]
        
        if not pending_jobs:
            messagebox.showinfo(
                "Info", 
                "No pending jobs to generate"
            )
            return
        
        self.generate_stickers_threaded(pending_jobs)
    
    def generate_stickers(self, job_folder, output_path):
        """Genera el PDF de stickers para un job usando datos del CSV"""
        job_id = os.path.basename(job_folder)  # Obtener job_id desde el principio
        
        try:
            is_valid, data = self.validate_job_folder(job_folder)
            if not is_valid:
                raise ValueError(data.get("error", "Invalid job folder"))

            # job_id ya está definido, no necesitamos obtenerlo de data
            qr_path = data["files"]["QR"]
            trusses_data = data["trusses_data"]

            # Crear y poblar PDF
            output_path = os.path.join(job_folder, f"Stickers_{job_id}.pdf")
            c = canvas.Canvas(output_path, pagesize=(100*mm, 30*mm))

            # Iterar a través de los datos del CSV
            for truss in trusses_data:
                code = truss['model'].split()[0]
                qty = truss["qty"]
                ply = truss["ply"]
                is_ply = ply > 1
                label = "PLY" if is_ply else "QTY"
                count = qty * ply if is_ply else qty
                batch = truss.get("batch", job_id)
                client = truss.get("client", "N/A")
                project = truss.get("project", "N/A")

                for idx in range(1, count + 1):
                    self.draw_sticker(
                        c,
                        job_id=batch,  # Usar el batch como ID del job
                        truss_model=truss["model"],
                        truss_type=truss["type"],
                        label_type=label,
                        current_num=idx,
                        total_num=count,
                        qr_path=qr_path,
                        client=client,
                        project=project
                    )
                    c.showPage()

            c.save()

            # Generar el resumen
            self.generate_stickers_summary(job_id, trusses_data, job_folder)

            # Verificar archivos
            summary_path = os.path.join(job_folder, f"{job_id}_Stickers_Summary.txt")
            if not (os.path.exists(output_path) and os.path.exists(summary_path)):
                raise ValueError("No se pudieron crear los archivos de stickers")

            self.update_job_status(job_id, "completed")

        except Exception as e:
            self.update_job_status(job_id, "error")
            raise ValueError(f"Error generating stickers: {e}")

    def _show_generation_result(self, success, total, errors):
        """Muestra el resultado de la generación múltiple"""
        if total == 0:
            return
        
        if success == total:
            msg = f"Successfully generated all {total} jobs!"
            messagebox.showinfo("Success", msg)
        elif success == 0:
            msg = f"Failed to generate all {total} jobs.\n\nErrors:\n" + "\n".join(errors)
            messagebox.showerror("All Failed", msg)
        else:
            msg = (f"Generated {success} of {total} jobs.\n\n"
                f"Successful: {success}\n"
                f"Failed: {total-success}\n\n"
                "Error details:\n" + "\n".join(errors))
            messagebox.showwarning("Partial Success", msg)

    def update_job_status(self, job_id, new_status):
        """Actualiza el estado de un job en el Treeview"""
        for item in self.tree.get_children():
            if self.tree.item(item, "values")[0] == job_id:
                self.tree.item(
                    item,
                    values=(
                        job_id,
                        self.tree.item(item, "values")[1],
                        new_status.capitalize(),
                        datetime.now().strftime('%Y-%m-%d %H:%M')
                    ),
                    tags=(new_status,)
                )
                break

    def draw_sticker(self, c, job_id, truss_model, truss_type, label_type, current_num, total_num, qr_path, client="N/A", project="N/A"):
        """Dibuja un sticker individual en el PDF"""
        width, height = 100*mm, 30*mm
        left_margin = right_margin = top_margin = bottom_margin = 2*mm

        # Top row: Job ID, Client, QTY/PLY
        c.setFont("Helvetica-Bold", 10)
        top_text_y = height - top_margin - 2.7*mm

        # Job ID (izquierda)
        c.drawString(left_margin + 0.5*mm, top_text_y, job_id)

        # Cliente (centrado)
        client = client[:22] + "..." if len(client) > 25 else client
        c.setFont("Helvetica-Bold", 9)
        client_x = (width - c.stringWidth(client, "Helvetica-Bold", 9)) / 2
        c.drawString(client_x, top_text_y, client)

        # QTY / PLY (derecha)
        c.setFont("Helvetica-Bold", 7)
        qty_text = f"{label_type}: {current_num:02d} of {total_num:02d}"
        qty_x = width - right_margin - c.stringWidth(qty_text, "Helvetica-Bold", 7) - 3.5*mm
        c.drawString(qty_x, top_text_y, qty_text)

        # Proyecto (debajo del cliente)
        project = project[:22] + "..." if len(project) > 25 else project
        c.setFont("Helvetica", 7)
        project_x = (width - c.stringWidth(project, "Helvetica", 7)) / 2
        c.drawString(project_x, top_text_y - 2.5*mm, project)

        # Truss code grande y centrado
        truss_code = truss_model.split()[0] if truss_model else ""
        
        # Tamaño de fuente dinámico
        if len(truss_code) <= 4:
            font_size = 48
        elif len(truss_code) == 5:
            font_size = 42
        elif len(truss_code) == 6:
            font_size = 36
        else:
            font_size = 30

        c.setFont("Helvetica-Bold", font_size)
        text_x = (width - c.stringWidth(truss_code, "Helvetica-Bold", font_size)) / 2
        text_y = height * 0.28
        c.drawString(text_x, text_y, truss_code)

        # Token QR (derecha)
        prefix = "Q" if label_type == "QTY" else "P"
        sticker_type = f"{prefix}{current_num:02d}/{total_num:02d}"
        token_data = self.register_sticker_token(job_id, truss_code, sticker_type)
        token = token_data['token']
        qr_data = f"{job_id}|{truss_code}|{sticker_type}|{token}"

        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=1)
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        temp_qr_path = os.path.join(tempfile.gettempdir(), f"temp_qr_{job_id}_{truss_code}_{current_num}.png")
        qr_img.save(temp_qr_path)

        qr_size = 20*mm
        qr_x = width - right_margin - qr_size - 1.5*mm
        qr_y = height - top_margin - qr_size - 3*mm
        c.drawImage(temp_qr_path, qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
        os.remove(temp_qr_path)

        # QR del Drive (izquierda)
        try:
            if os.path.exists(qr_path):
                qr_svg = svg2rlg(qr_path)
                original_size = max(qr_svg.width, qr_svg.height)
                scale_factor = (20*mm) / original_size
                drive_qr_x = left_margin
                drive_qr_y = height - top_margin - 23*mm

                c.saveState()
                c.translate(drive_qr_x, drive_qr_y)
                c.scale(scale_factor, scale_factor)
                renderPDF.draw(qr_svg, c, 0, 0)
                c.restoreState()
        except Exception as e:
            print(f"Error al dibujar QR Drive: {e}")

        # Footer
        footer_y = bottom_margin + 1.5*mm

        # Dirección centrada
        c.setFont("Helvetica-Bold", 5.5)
        company_text = "Tindell's Inc. - 2644 Byington Solway Rd, Knoxville"
        company_x = (width - c.stringWidth(company_text, "Helvetica-Bold", 5.5)) / 2
        c.drawString(company_x, footer_y, company_text)

        # "Tindell's use only" (bajo QR token)
        c.setFont("Helvetica", 4)
        footer_text = "TINDELL'S USE ONLY"
        footer_x = qr_x + (qr_size - c.stringWidth(footer_text, "Helvetica", 4)) / 2
        c.drawString(footer_x, footer_y, footer_text)

        # "Jobsite Package" (bajo QR Drive)
        js_text = "JOBSITE PACKAGE"
        c.setFont("Helvetica", 4)
        js_width = c.stringWidth(js_text, "Helvetica", 4)
        qr_drive_center_x = drive_qr_x + (20*mm / 2)
        js_x = qr_drive_center_x - (js_width / 2)
        c.drawString(js_x, footer_y, js_text)

        # Borde del sticker
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setLineWidth(0.5)
        c.rect(0, 0, width, height)

    def _open_selected_batches(self, selected_jobs, selected_batches):
        """Abre PDFs filtrados con el visor predeterminado"""
        for item in selected_jobs:
            job_id = self.tree.item(item, "values")[0]
            pdf_path = os.path.join(self.folder_path, job_id, f"Stickers_{job_id}.pdf")
            
            if not os.path.exists(pdf_path):
                messagebox.showwarning("Missing", f"PDF not found for {job_id}")
                continue

            try:
                from PyPDF2 import PdfReader, PdfWriter
                reader = PdfReader(pdf_path)
                writer = PdfWriter()

                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if any(batch in text for batch in selected_batches):
                        writer.add_page(page)

                if not writer.pages:
                    messagebox.showinfo("No Match", f"No matching stickers found in {job_id}")
                    continue

                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_pdf:
                    writer.write(temp_pdf)
                    temp_path = temp_pdf.name

                # Abrir con el visor predeterminado
                if os.name == 'nt':  # Windows
                    os.startfile(temp_path)
                elif os.name == 'darwin':  # macOS
                    import subprocess
                    subprocess.run(['open', temp_path])
                else:  # Linux
                    import subprocess
                    subprocess.run(['xdg-open', temp_path])

            except Exception as e:
                messagebox.showerror("Error", f"Error processing {job_id}:\n{e}")

    def generate_stickers_summary(self, job_id, trusses_data, output_folder):
        """Genera archivo de resumen de stickers con campo Batch y estado de impresión"""
        import os, pandas as pd

        summary_path = os.path.join(output_folder, f"{job_id}_Stickers_Summary.txt")
        csv_path     = os.path.join(output_folder, f"{job_id}.csv")
        csv_data = {}
        
        if os.path.exists(csv_path):
            df = pd.read_csv(csv_path)
        else:
            df = None

        with open(summary_path, 'w', encoding='utf-8') as f:

            total = qty_count = ply_count = 0

            if df is not None:
                # Recorremos solo los TrsName que aparecen en el CSV, en el mismo orden
                for _, row in df.iterrows():
                    code = row['TrsName'].strip()
                    batch = row.get('Batch', job_id).strip()
                    truss = next((t for t in trusses_data if t['model'].split()[0] == code), None)
                    if not truss:
                        continue  # si el código no estaba en el PDF, lo saltamos

                    truss_type = truss['type'][:22]
                    qty  = truss["qty"]
                    ply  = truss["ply"]
                    is_ply = ply > 1
                    label  = "PLY" if is_ply else "QTY"
                    count  = qty * ply if is_ply else qty

                    # Contadores
                    if is_ply:
                        ply_count += count
                    else:
                        qty_count += count
                    total += count

                    for i in range(1, count + 1):
                        sticker_type = f"{label[0]}{i:02d}/{count:02d}"
                        key = f"{batch}|{code}|{sticker_type}"
                        if key not in self.sticker_tokens:
                            self.register_sticker_token(job_id, code, sticker_type)
                        token   = self.sticker_tokens[key]['token']
                        printed = "Yes" if self.sticker_tokens[key].get('printed') else "No"

                        f.write(
                            f"| {job_id:<8} | {code:<6} | {truss_type:<22} | "
                            f"{label} {i:02d} of {count:02d} | {token:<12} | {batch:<10} | {printed:<7} |\n"
                        )
            else:
                
                for truss in trusses_data:
                    code = truss["model"].split()[0]
                    truss_type = ' '.join(truss["model"].split()[1:])[:22]
                    qty  = truss["qty"]
                    ply  = truss["ply"]
                    is_ply = ply > 1
                    label  = "PLY" if is_ply else "QTY"
                    count  = qty * ply if is_ply else qty
                    batch  = job_id  

                    # Contadores
                    if is_ply:
                        ply_count += count
                    else:
                        qty_count += count
                    total += count

                    for i in range(1, count + 1):
                        sticker_type = f"{label[0]}{i:02d}/{count:02d}"
                        key = f"{batch}|{code}|{sticker_type}"
                        if key not in self.sticker_tokens:
                            self.register_sticker_token(job_id, code, sticker_type)
                        token   = self.sticker_tokens[key]['token']
                        printed = "Yes" if self.sticker_tokens[key].get('printed') else "No"

                        f.write(
                            f"| {job_id:<8} | {code:<6} | {truss_type:<22} | "
                            f"{label} {i:02d} of {count:02d} | {token:<12} | {batch:<10} | {printed:<7} |\n"
                        )

            # Totales al final
            f.write("-"*90 + "\n")
            f.write(f"TOTAL STICKERS: {total} (QTY: {qty_count} | PLY: {ply_count})".center(90) + "\n")

        print(f"Resumen generado: {summary_path}")

    def register_sticker_token(self, job_id, truss_code, sticker_type, token=None):
        """Genera tokens sin guardarlos en JSON (solo en memoria mientras la app está abierta)."""
        if token is None:
            token = uuid.uuid4().hex[:12]  # TOKEEEEEN!!!
        
        key = f"{job_id}|{truss_code}|{sticker_type}"
        self.sticker_tokens[key] = {
            'token': token,
            'scanned': False,
            'timestamp': datetime.now().isoformat()
        }
        return self.sticker_tokens[key]

    def print_selected(self):
        """Imprime los stickers seleccionados mostrando el diálogo de impresión de Windows"""
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "Please select at least one job")
            return
        
        for item in selected:
            job_id = self.tree.item(item, "values")[0]
            pdf_path = os.path.join(
                self.folder_path, 
                job_id, 
                f"Stickers_{job_id}.pdf"
            )
            
            if os.path.exists(pdf_path):
                try:
                    if os.name == 'nt':  
                        import subprocess
                        
                        absolute_path = os.path.abspath(pdf_path)
                        subprocess.run(f'start "" "{absolute_path}"', shell=True)
                    elif os.name == 'posix':  # macOS/Linux
                        import subprocess
                        subprocess.run(['lp', pdf_path])
                except Exception as e:
                    messagebox.showerror(
                        "Print Error",
                        f"Failed to print {job_id}:\n{str(e)}"
                    )
            else:
                messagebox.showwarning(
                    "File Not Found",
                    f"No sticker file found for {job_id}"
                )

    def recover_tokens_from_summary(self, job_folder, job_id):
        """Restaura tokens desde summary con formato JobID|Batch|..."""
        summary_path = os.path.join(job_folder, f"{job_id}_Stickers_Summary.txt")
        if not os.path.exists(summary_path):
            return

        try:
            with open(summary_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.startswith("|") and "TOKEN" not in line:
                        parts = [p.strip() for p in line.strip().split("|")]
                        if len(parts) >= 7:  
                            batch = parts[5]  
                            truss_code = parts[2]
                            cantidad = parts[3]
                            
                            try:
                                # Primero intentamos con el formato "QTY 01 of 02"
                                if " of " in cantidad:
                                    cantidad_parts = cantidad.split()
                                    if len(cantidad_parts) == 4:
                                        label, index_str, _, total_str = cantidad_parts
                                    else:
                                        continue
                                else:
                                    # Si no, intentamos con formato "QTY 01/02" o similar
                                    cantidad_parts = cantidad.split()
                                    if len(cantidad_parts) == 2:
                                        label, index_total = cantidad_parts
                                        if '/' in index_total:
                                            index_str, total_str = index_total.split('/')
                                        else:
                                            continue
                                    else:
                                        continue
                                
                                # Validamos que los números sean dígitos
                                if not (index_str.isdigit() and total_str.isdigit()):
                                    continue
                                    
                                sticker_type = f"{label[0]}{index_str.zfill(2)}/{total_str.zfill(2)}"
                                token = parts[4]
                                
                                # Formato clave: JobID|Batch|TrussCode|Type
                                key = f"{job_id}|{batch}|{truss_code}|{sticker_type}"
                                
                                self.sticker_tokens[key] = {
                                    'token': token,
                                    'printed': parts[6].strip().lower() == "yes",
                                    'timestamp': datetime.now().isoformat()
                                }
                            except Exception as e:
                                print(f"[WARNING] Skipping line due to parsing error: {line.strip()}")
                                continue
        except Exception as e:
            print(f"[ERROR] Loading summary for {job_id}: {e}")

    # ---------------------------
    # FUNCIONES AUXILIARES
    # ---------------------------

    def on_mousewheel(self, event):
        """Permite hacer scroll con la rueda del ratón"""
        if event.num == 4 or event.delta > 0:  # Scroll up 
            self.tree.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:  # Scroll down 
            self.tree.yview_scroll(1, "units")

    def show_progress(self, message):
        """Muestra ventana de progreso"""
        self.progress_window = tk.Toplevel(self.root)
        self.progress_window.title("Processing...")
        self.progress_window.geometry("400x120")
        self.progress_window.resizable(False, False)
        
        # Centrar ventana
        self.progress_window.update_idletasks()
        width = self.progress_window.winfo_width()
        height = self.progress_window.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.progress_window.geometry(f"+{x}+{y}")
        
        # Widgets
        ttk.Label(
            self.progress_window, 
            text=message,
            font=('Helvetica', 10)
        ).pack(pady=10)
        
        self.progress_bar = ttk.Progressbar(
            self.progress_window, 
            orient=tk.HORIZONTAL, 
            length=300, 
            mode='determinate'
        )
        self.progress_bar.pack(pady=5)
        
        self.progress_label = ttk.Label(
            self.progress_window,
            text="Starting...",
            font=('Helvetica', 9)
        )
        self.progress_label.pack(pady=5)
    
    def update_progress(self, value, message):
        """Actualiza la barra de progreso"""
        if hasattr(self, 'progress_window'):
            self.progress_bar['value'] = value
            self.progress_label.config(text=message)
            self.progress_window.update()
    
    def hide_progress(self):
        """Cierra la ventana de progreso"""
        if hasattr(self, 'progress_window'):
            self.progress_window.destroy()
    
    def on_entry_click(self, event):
        """Maneja el evento click en la barra de búsqueda"""
        if self.search_entry.get() == "Search... (use 'status:' for status filter)":
            self.search_entry.delete(0, tk.END)
            self.search_entry.config(foreground='black')
    
    def on_focusout(self, event):
        """Maneja el evento focusout en la barra de búsqueda"""
        if not self.search_entry.get():
            self.search_entry.insert(0, "Search... (use 'status:' for status filter)")
            self.search_entry.config(foreground='grey')
    
    def filter_tree(self):
        """Filtra los jobs en el Treeview"""
        search_text = self.search_var.get().lower()
        
        # Primero mostrar todos los items
        for item in self.tree.get_children():
            self.tree.reattach(item, '', 'end')
        
        # Luego aplicar el filtro
        if search_text and search_text != "search... (use 'status:' for status filter)":
            for item in self.tree.get_children():
                values = [str(v).lower() for v in self.tree.item(item, "values")]
                
                if search_text.startswith("status:"):
                    status_filter = search_text.replace("status:", "").strip()
                    if status_filter not in values[2].lower():
                        self.tree.detach(item)
                else:
                    if not any(search_text in v for v in values):
                        self.tree.detach(item)
    
    def clear_search(self):
        """Versión definitiva que funciona como en Upload Manager"""
        self.search_var.set("")
        self.search_entry.delete(0, tk.END)
        self.search_entry.insert(0, "Search... (use 'status:' for status filter)")
        self.search_entry.config(foreground='grey')
        
        # Limpiar elementos ocultos, pero mantener Completed oculto por defecto
        for status_type in self.hidden_items:
            if status_type != 'Completed':
                self.hidden_items[status_type] = []
        
        # Reconstruir el Treeview (mostrando todo excepto Completed)
        self.rebuild_treeview()

    def rebuild_treeview(self):
        """Reconstruye el Treeview de forma optimizada usando caching completo"""
        import os, re
        from datetime import datetime

        # 1) Carga cache previo
        cache = load_cache()

        # 2) Limpia el Treeview
        self.tree.delete(*self.tree.get_children())
        if not self.folder_path:
            return

        # 3) Itera todas las carpetas de job con caching completo
        job_folders = [f for f in os.listdir(self.folder_path) if re.match(r'^\d{7}$', f)]
        
        for job_folder in job_folders:
            job_path = os.path.join(self.folder_path, job_folder)
            
            # Usar validación cached
            is_valid, validation_data = self.validate_job_folder_cached(job_path)
            status = validation_data["status"]
            status_display = status.capitalize()

            # Calcular cantidad display
            if status in ("pending", "completed"):
                if "trusses_data" in validation_data and validation_data["trusses_data"]:
                    total_count = 0
                    for truss in validation_data["trusses_data"]:
                        qty = truss["qty"]
                        ply = truss["ply"]
                        total_count += self.calculate_sticker_count(qty, ply)
                    qty_display = str(total_count)
                else:
                    qty_display = "Invalid"
            else:
                qty_display = validation_data.get("error", "Error").replace("Faltan archivos:", "Missing:")

            # Verificar si este status está oculto
            is_hidden = status_display in self.hidden_items and self.hidden_items[status_display]
            
            # Insertar solo si no está oculto
            if not is_hidden:
                item = self.tree.insert(
                    "", "end",
                    values=(
                        job_folder,
                        qty_display,
                        status_display,
                        datetime.now().strftime('%Y-%m-%d %H:%M')
                    ),
                    tags=(status,)
                )
    
    def show_context_menu(self, event):
        """Muestra el menú contextual al hacer clic derecho; selecciona el item bajo el cursor."""
        item = self.tree.identify_row(event.y)

        if item and item not in self.tree.selection():
            self.tree.selection_set(item)

        # Limpiar menú previo
        self.context_menu.delete(0, tk.END)
        
        # --- SUBMENÚ HIDE (aprender para mejorarlo)---
        hide_submenu = tk.Menu(self.context_menu, tearoff=0)
        
        # Opciones de ocultar
        hide_submenu.add_command(label="Pending", command=lambda: self.hide_status("Pending"))
        hide_submenu.add_command(label="Completed", command=lambda: self.hide_status("Completed"))
        hide_submenu.add_command(label="Error", command=lambda: self.hide_status("Error"))
        hide_submenu.add_separator()
        
        # --- SUBMENÚ SHOW HIDDEN ---
        show_hidden_submenu = tk.Menu(hide_submenu, tearoff=0)
        
        if self.hidden_items['Pending']:
            show_hidden_submenu.add_command(label="Show Pending", command=lambda: self.show_hidden_status("Pending"))
        if self.hidden_items['Completed']:
            show_hidden_submenu.add_command(label="Show Completed", command=lambda: self.show_hidden_status("Completed"))
        if self.hidden_items['Error']:
            show_hidden_submenu.add_command(label="Show Error", command=lambda: self.show_hidden_status("Error"))
        
        if any(self.hidden_items.values()):
            hide_submenu.add_cascade(label="Show Hidden", menu=show_hidden_submenu)
        
        hide_submenu.add_command(label="Show All", command=self.show_all)
        
        # Agregar submenú al menú principal
        self.context_menu.add_cascade(label="Hide/Show", menu=hide_submenu)
        # --- FIN SUBMENÚ HIDE ---

        if item:
            job_id = self.tree.item(item, "values")[0]
            job_path = os.path.join(self.folder_path, job_id)

            self.context_menu.add_separator()
            self.context_menu.add_command(label="Open job folder", command=lambda: self.open_job_folder(job_path))
            self.context_menu.add_command(label="Revalidate job", command=lambda: self.revalidate_job(job_id))
            self.context_menu.add_command(label="Generate stickers", command=lambda: self.generate_stickers_threaded([job_id]))
            self.context_menu.add_command(label="Copy Job ID", command=lambda: self.root.clipboard_append(job_id))
        else:
            # Clic en área vacía: acciones globales
            self.context_menu.add_separator()
            self.context_menu.add_command(label="Refresh", command=self.rebuild_treeview)
            self.context_menu.add_command(label="Clear selection", command=lambda: self.tree.selection_remove(self.tree.selection()))

        # Mostrar menú usando tk_popup para mejor compatibilidad
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def show_hidden_status(self, status_type):
        """Muestra los items ocultos de un tipo específico"""
        # Limpiar la lista de elementos ocultos para este tipo
        self.hidden_items[status_type] = []
        
        # Reconstruir el treeview para mostrar este tipo
        self.rebuild_treeview()

    def show_all(self):
        """Show all jobs (including all status types)"""
        # Limpiar todos los elementos ocultos
        for status_type in self.hidden_items:
            self.hidden_items[status_type] = []

        self.rebuild_treeview()

    def hide_status(self, status_type):
        """Hide jobs with the specified status"""
        # Para Completed, usar el marcador especial
        if status_type == "Completed":
            self.hidden_items['Completed'] = ['completed_default']
            # Ocultar los elementos Completed actuales
            for item in self.tree.get_children():
                values = self.tree.item(item, 'values')
                if len(values) > 2 and values[2] == "Completed":
                    self.tree.detach(item)
        else:
            # Para otros status, ocultar los elementos actuales
            self.hidden_items[status_type] = []
            for item in self.tree.get_children():
                values = self.tree.item(item, 'values')
                if len(values) > 2 and values[2] == status_type:
                    self.hidden_items[status_type].append(item)
                    self.tree.detach(item)

    def show_all_hidden(self):
        """Show all hidden items except Completed by default"""
        # Primero mostrar todos los elementos
        for item in self.tree.get_children():
            self.tree.reattach(item, '', 'end')
        
        # Limpiar la lista
        for status_type in self.hidden_items:
            if status_type != 'Completed':
                self.hidden_items[status_type] = []
        
        # Ahora ocultar solo Completed (estado por defecto)
        self.hide_status("Completed")

    def show_all(self):
        """Show all jobs (including Completed)"""
        for status_type in self.hidden_items:
            self.hidden_items[status_type] = []
        
        self.rebuild_treeview()

    def open_job_folder(self, path):
        """Abre la carpeta del job en el explorador de archivos"""
        try:
            if os.name == 'nt':  # Windows
                os.startfile(path)
            elif os.name == 'darwin':  # macOS
                subprocess.run(['open', path])
            else:  # Linux
                subprocess.run(['xdg-open', path])
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo abrir la carpeta:\n{e}")

    def revalidate_job(self, job_id):
        """Revalida un único job usando el mismo cálculo que rebuild_treeview/validate."""
        import os
        from datetime import datetime

        job_path = os.path.join(self.folder_path, job_id)
        is_valid, validation_data = self.validate_job_folder(job_path)

        status = validation_data["status"]      # "pending" / "completed" / "error"
        status_display = status.capitalize()    # "Pending", "Completed", "Error"

        if status in ("pending", "completed"):
            # Aquí usamos exactamente el mismo cálculo que en rebuild_treeview
            expected = self.compute_expected_count(job_path)
            qty_display = str(expected) if expected is not None else "Invalid"
        else:
            qty_display = validation_data["error"].replace("Faltan archivos:", "Missing:")

        # Actualiza solo la fila correspondiente en el Treeview
        for item in self.tree.get_children():
            if self.tree.item(item, "values")[0] == job_id:
                self.tree.item(
                    item,
                    values=(
                        job_id,
                        qty_display,
                        status_display,
                        datetime.now().strftime('%Y-%m-%d %H:%M')
                    ),
                    tags=(status,)
                )
                break


# ---------------------------
# EJECUCIÓN PRINCIPAL
# ---------------------------
if __name__ == "__main__":
    def on_close():
        root.destroy()

    root = tk.Tk()
    app = StickerManager(root)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()