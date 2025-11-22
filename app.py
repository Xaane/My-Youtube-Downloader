import os
import tempfile 
import shutil 
import time
import uuid 
import threading 
from flask import Flask, render_template, request, send_file, redirect, url_for, after_this_request, jsonify, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

# --- Initialisation de l'application Flask ---
app = Flask(__name__)

# Dictionnaire global pour stocker l'état des téléchargements (In-memory, non persistant)
DOWNLOAD_JOBS = {} 

# --- Fonctions Utilitaires ---
# Conservation uniquement des fonctions encore utilisées.

def format_bytes(bytes_value):
    """Formate les octets pour l'affichage de la vitesse/taille totale dans le modal de progression."""
    if bytes_value is None: return "Taille inconnue"
    try: bytes_value = int(bytes_value)
    except (ValueError, TypeError): return "Taille inconnue"
    if bytes_value < 1024: return f"{bytes_value} B"
    elif bytes_value < 1024**2: return f"{bytes_value / 1024:.2f} KB"
    elif bytes_value < 1024**3: return f"{bytes_value / 1024**2:.2f} MB"
    else: return f"{bytes_value / 1024**3:.2f} GB"

def format_large_number(number):
    if number is None: return "N/A"
    try: return "{:,}".format(int(number)).replace(",", " ")
    except ValueError: return str(number)

def format_duration(duration_seconds):
    if not duration_seconds: return "N/A"
    try: duration_seconds = int(duration_seconds)
    except (ValueError, TypeError): return "N/A"
    hours = duration_seconds // 3600
    minutes = (duration_seconds % 3600) // 60
    seconds = duration_seconds % 60
    if hours > 0: return f"{hours}h {minutes:02}m {seconds:02}s"
    return f"{minutes:02}m {seconds:02}s"


def extract_and_filter_formats(info_dict, video_duration):
    """Extrait les informations essentielles et filtre les meilleurs formats vidéo par hauteur."""
    video_info = {
        'url': info_dict.get('webpage_url'),
        'title': info_dict.get('title', 'Titre Inconnu'),
        'channel': info_dict.get('uploader', 'Chaîne Inconnue'),
        'view_count': format_large_number(info_dict.get('view_count')),
        'duration': format_duration(video_duration),
        'thumbnail': info_dict.get('thumbnail'),
        'description': info_dict.get('description', '').split('\n')[0][:150] + '...'
    }

    best_video_per_height = {}
    for f in info_dict.get('formats', []):
        height = f.get('height')
        tbr = f.get('tbr', 0) 
        if not height or f.get('vcodec') == 'none' or f.get('acodec') == 'none': continue
        if best_video_per_height.get(height) is None or tbr > best_video_per_height.get(height).get('tbr', 0):
            best_video_per_height[height] = f

    # Option par défaut
    available_formats = [{
        'id': 'best', 'resolution': 'Automatique', 'fps': 'N/A', 'ext': 'MP4',
        'status': 'Meilleure qualité combinée (par défaut)'
    }]

    for height in sorted(best_video_per_height.keys(), reverse=True):
        f = best_video_per_height[height]
        
        is_muxed = (f.get('acodec') != 'none')
        available_formats.append({
            'id': f.get('format_id'), 
            'resolution': f"{height}p",
            'fps': f.get('fps', 'N/A'),
            'ext': f.get('ext', 'unk').upper(),
            'status': "Mixé (Vidéo + Audio)"
        })

    return video_info, available_formats


# --- Hook de Progression yt-dlp ---

def ydl_progress_hook(d, job_id):
    """
    Met à jour l'état du téléchargement et vérifie si l'utilisateur a demandé l'annulation.
    """
    global DOWNLOAD_JOBS
    if job_id not in DOWNLOAD_JOBS: return

    job_data = DOWNLOAD_JOBS[job_id]

    # Vérification d'annulation
    if job_data.get('cancelled'):
        d['status'] = 'error' 
        job_data['status'] = 'Annulé'
        job_data['error'] = 'Téléchargement annulé par l\'utilisateur.'
        # Lève une exception pour forcer l'arrêt du processus yt-dlp
        raise DownloadError("Download cancelled by user hook.")

    if d['status'] == 'downloading':
        # Logique de calcul de progression (inchangée)
        downloaded = d.get('downloaded_bytes')
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        
        if downloaded and total and total > 0:
            job_data['progress'] = (downloaded / total) * 100
        elif d.get('fraction_downloaded') is not None:
             job_data['progress'] = d.get('fraction_downloaded') * 100
        else:
             job_data['progress'] = job_data.get('progress', 0) 

        job_data['progress'] = max(0, min(100, job_data['progress'])) 
        
        job_data['speed'] = format_bytes(d.get('speed')) + '/s' if d.get('speed') else 'N/A'
        job_data['total_bytes'] = format_bytes(total)
        job_data['status'] = 'Téléchargement de la vidéo...'
        
    elif d['status'] == 'finished':
        # Post-traitement: Muxage/Zippage et préparation du chemin final
        job_data['status'] = 'Post-traitement / Muxage...'
        download_folder = job_data['temp_dir'].name
        is_playlist = job_data.get('is_playlist', False)
        
        try:
            if is_playlist:
                job_data['status'] = 'Archivage ZIP en cours...'
                playlist_dirs = [d for d in os.listdir(download_folder) if os.path.isdir(os.path.join(download_folder, d))]
                
                if not playlist_dirs: raise Exception("Aucun dossier de playlist n'a été créé.")
                
                playlist_title = playlist_dirs[0]
                zip_base = os.path.join(download_folder, playlist_title) 
                
                output_path = shutil.make_archive(
                    base_name=zip_base, 
                    format='zip', 
                    root_dir=download_folder,
                    base_dir=playlist_title
                )
                filename = f"{playlist_title}.zip"
            else:
                downloaded_files = [f for f in os.listdir(download_folder) if not f.startswith('.')]
                if not downloaded_files: raise Exception("Le téléchargement n'a produit aucun fichier.")
                
                output_path = os.path.join(download_folder, downloaded_files[0])
                filename = downloaded_files[0]
                
            job_data['output_path'] = output_path
            job_data['filename'] = filename
            job_data['progress'] = 100
            job_data['status'] = 'Prêt à l\'envoi'
            
        except Exception as e:
            job_data['status'] = 'Erreur'
            job_data['error'] = f"Erreur de post-traitement: {str(e)}"
            app.logger.error(f"Erreur de post-traitement du job {job_id}: {e}")
            
    elif d['status'] == 'error':
        if not job_data.get('cancelled'):
            job_data['progress'] = -1
            job_data['status'] = 'Erreur'
            job_data['error'] = 'Erreur lors du téléchargement par yt-dlp.'


# --- Fonction de Téléchargement Exécutée en Thread ---

def download_video_thread(url, format_code, is_playlist, job_id):
    """
    Exécute le téléchargement réel par yt-dlp dans un thread séparé en utilisant le format_code exact.
    """
    global DOWNLOAD_JOBS
    temp_dir = None
    
    try:
        temp_dir = tempfile.TemporaryDirectory()
        download_folder = temp_dir.name
        DOWNLOAD_JOBS[job_id]['temp_dir'] = temp_dir 
        DOWNLOAD_JOBS[job_id]['status'] = 'Configuration yt-dlp...'
        
        
        if is_playlist:
            outtmpl = os.path.join(download_folder, '%(playlist)s/%(playlist_index)s - %(title)s.%(ext)s')
            ydl_opts = {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best', # Meilleur muxage MP4 par défaut pour playlist
                'outtmpl': outtmpl, 
                'merge_output_format': 'mp4',
                'ignoreerrors': True, 'quiet': True,'no_warnings': True,
                'progress_hooks': [lambda d: ydl_progress_hook(d, job_id)],
            }
        else:
            outtmpl = os.path.join(download_folder, '%(title)s.%(ext)s')
            
            if format_code == 'best':
                 ydl_format = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'
            else:
                 # Utilisation de l'ID spécifique + le meilleur audio pour les formats vidéo seuls.
                 ydl_format = f"{format_code}+bestaudio/best"
            
            ydl_opts = {
                'format': ydl_format, 
                'outtmpl': outtmpl, 
                'merge_output_format': 'mp4',
                'quiet': True,'no_warnings': True,
                'progress_hooks': [lambda d: ydl_progress_hook(d, job_id)],
            }
            
        DOWNLOAD_JOBS[job_id]['status'] = 'Démarrage du téléchargement...'

        with YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            
    except DownloadError as e:
        if not DOWNLOAD_JOBS[job_id].get('cancelled'):
            DOWNLOAD_JOBS[job_id]['status'] = 'Erreur'
            DOWNLOAD_JOBS[job_id]['error'] = f"Erreur de téléchargement yt-dlp: {e}"
        
    except Exception as e:
        DOWNLOAD_JOBS[job_id]['status'] = 'Erreur'
        DOWNLOAD_JOBS[job_id]['error'] = f"Erreur interne inattendue: {e}"
        
    finally:
        status = DOWNLOAD_JOBS.get(job_id, {}).get('status')
        if status in ['Erreur', 'Annulé'] and temp_dir:
            try:
                temp_dir.cleanup()
            except Exception:
                pass


# --- Routes Flask ---

@app.route('/', methods=['GET'])
def index():
    job_id_param = request.args.get('job_id') 
    url_analyzed_param = request.args.get('url_analyzed')
    
    return render_template('index.html', 
                           success=request.args.get('success'),
                           error=request.args.get('error'),
                           job_id=job_id_param,
                           url_analyzed=url_analyzed_param)


@app.route('/analyze', methods=['POST'])
def analyze():
    video_url = request.form.get('url')
    if not video_url:
        return redirect(url_for('index', error="Veuillez fournir une URL valide."))

    try:
        ydl_opts_info = {'quiet': True, 'simulate': True, 'playlist_items': '1:1'}
        with YoutubeDL(ydl_opts_info) as ydl:
            initial_info = ydl.extract_info(video_url, download=False)
            
    except DownloadError as e:
        return redirect(url_for('index', error=f"Erreur d'analyse: URL invalide ou vidéo indisponible. ({e})"))
    except Exception:
        return redirect(url_for('index', error="Une erreur serveur inattendue s'est produite lors de l'analyse."))

    is_playlist = initial_info.get('_type') == 'playlist'
    
    if is_playlist:
        playlist_data = {
            'is_playlist': True,
            'url': video_url,
            'title': initial_info.get('title', 'Playlist Inconnue'),
            'count': initial_info.get('playlist_count', 'N/A'),
            'thumbnail': initial_info.get('entries', [{}])[0].get('thumbnail') 
        }
        return render_template('index.html', playlist=playlist_data, url_analyzed=video_url)
    else:
        ydl_opts_info_full = {'quiet': True, 'simulate': True}
        with YoutubeDL(ydl_opts_info_full) as ydl:
            full_info = ydl.extract_info(video_url, download=False)
            video_duration = full_info.get('duration')

        video_info, available_formats = extract_and_filter_formats(full_info, video_duration)
        return render_template('index.html', video_info=video_info, formats=available_formats, url_analyzed=video_url)


@app.route('/start_download', methods=['POST'])
def start_download():
    url = request.form.get('url')
    format_code = request.form.get('format_code', 'best')
    is_playlist = request.form.get('is_playlist') == 'True'
    
    if not url:
        return redirect(url_for('index', error="URL de téléchargement manquante."))
    
    job_id = str(uuid.uuid4())
    
    DOWNLOAD_JOBS[job_id] = {
        'status': 'En attente...',
        'progress': 0,
        'is_playlist': is_playlist,
        'format_code': format_code,
        'error': None,
        'temp_dir': None,
        'output_path': None,
        'filename': None,
        'cancelled': False 
    }
    
    thread = threading.Thread(target=download_video_thread, 
                              args=(url, format_code, is_playlist, job_id))
    thread.start()
    
    # Utilisation du code 303 See Other pour maintenir l'état de la page d'analyse (Post/Redirect/Get pattern)
    response = Response(status=303)
    response.headers['Location'] = url_for('index', job_id=job_id, url_analyzed=url)
    return response

@app.route('/cancel_download/<job_id>', methods=['POST'])
def cancel_download(job_id):
    job_data = DOWNLOAD_JOBS.get(job_id)
    if job_data:
        job_data['cancelled'] = True
        job_data['status'] = 'Annulation en cours...'
        return jsonify({'status': 'Annulation demandée', 'message': 'Le processus sera arrêté sous peu.'}), 202
    
    return jsonify({'status': 'Erreur', 'message': 'Job introuvable ou déjà terminé.'}), 404


@app.route('/progress/<job_id>', methods=['GET'])
def get_progress(job_id):
    job_data = DOWNLOAD_JOBS.get(job_id)
    
    if not job_data:
        return jsonify({'status': 'Erreur', 'progress': -1, 'error': 'Job introuvable ou expiré.'}), 404

    if job_data['status'] == 'Annulé':
        return jsonify({
            'status': 'Annulé',
            'progress': -1,
            'error': job_data.get('error', 'Téléchargement annulé par l\'utilisateur.')
        })

    if job_data['status'] == 'Prêt à l\'envoi':
        return jsonify({
            'status': 'Terminé',
            'progress': 100,
            'download_url': url_for('serve_file', job_id=job_id)
        })
    
    if job_data['status'] == 'Erreur':
        return jsonify({
            'status': 'Erreur', 
            'progress': -1, 
            'error': job_data.get('error', 'Erreur inconnue lors du traitement.')
        })

    response = {
        'status': job_data['status'],
        'progress': round(job_data['progress'], 1), 
        'speed': job_data.get('speed', 'N/A'),
        'total_bytes': job_data.get('total_bytes', 'N/A')
    }
    
    return jsonify(response)


@app.route('/serve_file/<job_id>', methods=['GET'])
def serve_file(job_id):
    job_data = DOWNLOAD_JOBS.get(job_id)
    
    if not job_data or job_data['status'] != 'Prêt à l\'envoi' or not job_data.get('output_path'):
        app.logger.error(f"Tentative de service de fichier pour job {job_id} non prêt ou inexistant.")
        return "Fichier non prêt ou job introuvable.", 404 
        
    output_path = job_data['output_path']
    filename = job_data['filename']
    temp_dir = job_data['temp_dir']
    
    mimetype = 'application/zip' if filename.endswith('.zip') else 'application/octet-stream'

    @after_this_request
    def cleanup(response):
        """Nettoie le répertoire temporaire après que le fichier a été servi."""
        try:
            if temp_dir:
                # Augmentation du délai pour donner le temps aux gestionnaires de téléchargement externes.
                time.sleep(5) 
                temp_dir.cleanup()
            if job_id in DOWNLOAD_JOBS:
                del DOWNLOAD_JOBS[job_id]
        except Exception:
            pass
        return response

    return send_file(
        output_path,
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype 
    )


# if __name__ == '__main__':
#     # Utilisez use_reloader=False en environnement de thread.
#     app.run(debug=True, use_reloader=False)


if __name__ == '__main__':
    # '0.0.0.0' dit à Flask d'écouter sur TOUTES les interfaces réseau disponibles, 
    # y compris votre adresse IP locale.
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)