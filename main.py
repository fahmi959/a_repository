import firebase_admin
from firebase_admin import credentials, firestore, storage
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler
import os
import json
import io


from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload


from datetime import datetime
import pytz
import logging


# Setup basic logging configuration
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')



# Ambil kredensial dari variabel lingkungan
DRIVE_CREDENTIALS_JSON = os.getenv('DRIVE_CREDENTIALS')

if DRIVE_CREDENTIALS_JSON is None:
    logging.error('DRIVE_CREDENTIALS is not set.')
else:
    try:
        drive_credentials = json.loads(DRIVE_CREDENTIALS_JSON)
        logging.info('DRIVE_CREDENTIALS JSON is valid.')
    except json.JSONDecodeError:
        logging.error('Invalid JSON format in DRIVE_CREDENTIALS.')


# Load the JSON credentials from an environment variable
google_credentials = json.loads(os.environ.get('GOOGLE_CREDENTIALS'))

# Initialize Firebase Admin SDK
cred = credentials.Certificate(google_credentials)

    
# Inisialisasi Firebase
firebase_admin.initialize_app(
    cred,
    {
        'storageBucket': 'list-bot--telegram.appspot.com'  # Ganti dengan ID bucket Anda
    }
)

db = firestore.client()

# Dapatkan referensi bucket
bucket = storage.bucket()

# Token dari BotFather
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def authenticate_google_drive():
    try:
        credentials_info = json.loads(DRIVE_CREDENTIALS_JSON)
        credentials = service_account.Credentials.from_service_account_info(credentials_info)
        service = build('drive', 'v3', credentials=credentials)
        logging.info('Google Drive authenticated successfully.')
        return service
    except Exception as e:
        logging.error(f'Authentication error: {e}')
        return None

def upload_log_to_google_drive(file_path, folder_id):
    if not os.path.exists(file_path):
        logging.error(f'File {file_path} does not exist.')
        return

    logging.info(f'Uploading file {file_path} to Google Drive.')
    service = authenticate_google_drive()

    if service is None:
        logging.error('Google Drive service could not be authenticated.')
        return

    file_metadata = {
        'name': os.path.basename(file_path),
        'parents': [folder_id]
    }
    media = MediaFileUpload(file_path, resumable=True)

    try:
        # Search for existing files with the same name
        query = f"name='{os.path.basename(file_path)}' and '{folder_id}' in parents and trashed=false"
        existing_files = service.files().list(q=query, spaces='drive', fields='files(id)').execute().get('files', [])

        logging.info(f'Query result: {existing_files}')

        if existing_files:
            # If file exists, update it
            file_id = existing_files[0]['id']
            service.files().update(
                fileId=file_id,
                media_body=media
            ).execute()
            logging.info(f'Updated File ID: {file_id}')
        else:
            # If file does not exist, create a new one
            file_metadata['parents'] = [folder_id]
            file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            logging.info(f'Created File ID: {file.get("id")}')
    except Exception as e:
        logging.error(f'An error occurred during upload: {e}')




def start(update: Update, context: CallbackContext):
    user = update.message.from_user
    user_id = user.id
    username = user.username or "Tidak ada username"

    # Periksa apakah pengguna ter-banned
    banned_user_ref = db.collection('banned_users').document(str(user_id))
    if banned_user_ref.get().exists:
        try:
            context.bot.send_message(
                chat_id=user_id,
                text="Anda telah dibanned dan tidak dapat mendaftar lagi. Silakan hubungi kontak admin@bot.unnes kirimkan email dan kirimkan bukti skrinshot tanggal terakhir kali anda di banned untuk melakukan banding dan pengecekan terkait."
            )
        except Exception as e:
            print(f"Failed to send message: {e}")
        return

    # Simpan pengguna ke Firestore tanpa foto
    user_doc_ref = db.collection('users').document(str(user_id))
    user_doc_ref.set({
        'username': username,
        'photo': None,
        'status': 'registered'
    })

    # Ambil dan simpan foto profil jika tersedia
    try:
        profile_photos = context.bot.get_user_profile_photos(user_id)
        if profile_photos.total_count > 0:
            photo_id = profile_photos.photos[0][-1].file_id
            file = context.bot.get_file(photo_id)
            file.download('profile_photo.jpg')

            # Unggah gambar ke Firebase Storage
            blob = bucket.blob(f'profile_photos/{user_id}.jpg')
            blob.upload_from_filename('profile_photo.jpg')
            profile_photo_url = blob.public_url

            # Update Firestore dengan URL foto profil
            user_doc_ref.update({'photo': profile_photo_url})
    except Exception as e:
        print(f"Failed to handle profile photo: {e}")

    # Buat keyboard inline
    keyboard = [[
        InlineKeyboardButton("/search", callback_data='search'),
        InlineKeyboardButton("/next", callback_data='next')
    ], [
        InlineKeyboardButton("/stop", callback_data='stop'),
    ]]

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        context.bot.send_message(
            chat_id=user_id,
            text=("Anda telah terdaftar. Silakan gunakan perintah /search untuk mencari pasangan. "
                 "Gunakan tombol di bawah untuk menggunakan perintah bot."),
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Failed to send message: {e}")


# Untuk Mengetahui Update Riwayat Rekam jejak User
import hashlib

def update_user_info(user_id: str, username: str, photo_url: str):
    # Referensi ke dokumen pengguna di koleksi utama
    user_doc_ref = db.collection('users').document(str(user_id))
    user_doc_ref.update({'username': username, 'photo': photo_url})

    # Referensi ke subkoleksi riwayat pengguna
    history_ref = user_doc_ref.collection('history')
    
    # Tambahkan entri riwayat baru dengan timestamp
    history_ref.add({
        'username': username,
        'photo': photo_url,
        'timestamp': firestore.SERVER_TIMESTAMP
    })

    # Batched write untuk menghapus entri lama jika melebihi batas
    batch = db.batch()
    entries = history_ref.order_by('timestamp').limit_to_last(6).get()
    if len(entries) > 5:
        for entry in entries[:-5]:
            batch.delete(entry.reference)
    batch.commit()


def calculate_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, 'rb') as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    return hasher.hexdigest()



def get_last_photo_metadata(user_id: str) -> dict:
    user_ref = db.collection('users').document(str(user_id))
    user_doc = user_ref.get()
    if user_doc.exists:
        return user_doc.to_dict().get('last_photo', {})
    return {}

def update_last_photo_metadata(user_id: str, file_id: str, photo_url: str, file_hash: str):
    user_ref = db.collection('users').document(str(user_id))
    user_ref.update({
        'last_photo': {
            'file_id': file_id,
            'url': photo_url,
            'hash': file_hash
        }
    })


def handle_photo_update(user_id: str, context: CallbackContext):
    temp_file_name = f'{user_id}_temp.jpg'
    try:
        profile_photos = context.bot.get_user_profile_photos(user_id)
        if profile_photos.total_count > 0:
            new_photo = profile_photos.photos[0][-1]
            new_file_id = new_photo.file_id
            new_file = context.bot.get_file(new_file_id)
            
            # Ambil metadata foto terakhir
            last_photo_metadata = get_last_photo_metadata(user_id)
            last_file_hash = last_photo_metadata.get('hash', None)

            # Unduh foto baru untuk dihitung hash-nya
            new_file.download(temp_file_name)
            new_file_hash = calculate_hash(temp_file_name)
            
            # Periksa apakah foto baru berbeda dari foto terakhir
            if new_file_hash == last_file_hash:
                print("Foto tidak berubah. Tidak ada pembaruan.")
                return last_photo_metadata.get('url', None)

            # Foto berbeda, unggah foto baru
            increment = last_photo_metadata.get("timestamp", 0) + 1
            file_name = f'{user_id}_{increment}.jpg'
            blob = bucket.blob(f'profile_photos/{file_name}')
            blob.upload_from_filename(temp_file_name)
            profile_photo_url = blob.public_url

            # Update metadata foto terakhir
            update_last_photo_metadata(user_id, new_file_id, profile_photo_url, new_file_hash)

            return profile_photo_url
        return None
    except Exception as e:
        print(f"Error in handle_photo_update: {e}")
        raise
    finally:
        if os.path.exists(temp_file_name):
            os.remove(temp_file_name)  # Hapus file sementara setelah diunggah atau jika terjadi kesalahan



# Fungsi Mencari User
def search(update: Update, context: CallbackContext):
    # Menentukan ID pengguna berdasarkan tipe pembaruan
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    else:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text="Terjadi kesalahan.")
        return

    # Periksa apakah pengguna terdaftar
    user_ref = db.collection('users').document(str(user_id))
    user_doc = user_ref.get()

    if not user_doc.exists:
        context.bot.send_message(
            chat_id=user_id,
            text=
            "Anda harus mendaftar terlebih dahulu dengan menggunakan perintah /start."
        )
        return

    # Periksa apakah pengguna sudah terhubung dengan pasangan
    active_chat_ref = db.collection('active_chats').document(str(user_id))
    active_chat_doc = active_chat_ref.get()

    if active_chat_doc.exists:
        context.bot.send_message(
            chat_id=user_id,
            text=
            "Maaf, Anda masih terhubung dengan pasangan. Gunakan perintah /next untuk mencari pasangan baru."
        )
        return

    # Perbarui informasi pengguna di daftar tunggu jika ada
    waiting_ref = db.collection('waiting_users')
    waiting_users = waiting_ref.get()

    for waiting_user in waiting_users:
        if waiting_user.id == str(user_id):
            profile_photo_url = handle_photo_update(user_id, context)
            username = update.message.from_user.username or "Tidak ada username"
            if profile_photo_url:
                update_user_info(user_id, username, profile_photo_url)
            break

    # Periksa ulang daftar pengguna yang menunggu
    waiting_users = waiting_ref.get()
    if waiting_users:
        partner_id = waiting_users[0].id

        if partner_id == str(user_id):
            # Jangan pertemukan pengguna dengan dirinya sendiri
            context.bot.send_message(
                chat_id=user_id,
                text="Silakan Tunggu, Sedang Menemukan Pasangan....")
            return

        # Hapus pengguna dari daftar tunggu dan simpan pasangan
        db.collection('waiting_users').document(partner_id).delete()
        db.collection('active_chats').document(str(user_id)).set(
            {'partner': partner_id})
        db.collection('active_chats').document(str(partner_id)).set(
            {'partner': user_id})

        context.bot.send_message(
            chat_id=user_id, text="Pasangan ditemukan! Mulailah mengobrol.")
        context.bot.send_message(
            chat_id=partner_id, text="Pasangan ditemukan! Mulailah mengobrol.")
    else:
        # Tambahkan pengguna ke daftar tunggu
        db.collection('waiting_users').document(str(user_id)).set({})
        context.bot.send_message(chat_id=user_id,
                                 text="Menunggu pasangan. Mohon tunggu...")


# Fungsi untuk menghentikan chat
def stop_chat(update: Update, context: CallbackContext):
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    else:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text="Terjadi kesalahan.")
        return

    chat_ref = db.collection('active_chats').document(str(user_id))
    chat = chat_ref.get()

    if chat.exists:
        partner_id = chat.to_dict()['partner']
        db.collection('active_chats').document(str(user_id)).delete()
        db.collection('active_chats').document(str(partner_id)).delete()

        context.bot.send_message(
            chat_id=user_id,
            text=
            "Chat telah dihentikan. Mohon donasinya kakak 1000 rupiah juga berarti bagi kami di saweria.co/Unnesbot agar server kami tetap berjalan dengan baik."
        )
        context.bot.send_message(chat_id=partner_id,
                                 text="Pasangan Anda telah meninggalkan chat.")
    else:
        context.bot.send_message(chat_id=user_id,
                                 text="Anda tidak sedang dalam chat.")


def next_chat(update: Update, context: CallbackContext):
    # Mendapatkan user_id dari pesan atau callback_query
    if update.message:
        user_id = update.message.from_user.id
    elif update.callback_query:
        user_id = update.callback_query.from_user.id
    else:
        context.bot.send_message(chat_id=update.effective_chat.id,
                                 text="Terjadi kesalahan.")
        return


    # Perbarui informasi pengguna di daftar tunggu jika ada
    waiting_ref = db.collection('waiting_users')
    waiting_users = waiting_ref.get()

    for waiting_user in waiting_users:
        if waiting_user.id == str(user_id):
            # Ambil foto profil terbaru dan username
            profile_photo_url = handle_photo_update(user_id, context)
            username = update.message.from_user.username or "Tidak ada username"
            if profile_photo_url:
                update_user_info(user_id, username, profile_photo_url)
            break

  
    # Hentikan chat saat ini
    stop_chat(update, context)
    # Cari pasangan baru
    search(update, context)


def generate_unique_timestamp():
    from datetime import datetime
    return datetime.utcnow().strftime('%Y%m%d%H%M%S%f')


# Pengelola Pesan
MAX_LOG_SIZE_MB = 10
MAX_LOG_SIZE_BYTES = MAX_LOG_SIZE_MB * 1024 * 1024

def get_log_file_path(user_id):
    """Return the current log file path based on size and version."""
    base_path = '/tmp'
    log_file_prefix = f'{user_id}_chat_log'
    log_file_suffix = 1

    while True:
        log_file_path = os.path.join(base_path, f'{log_file_prefix}_{log_file_suffix}.txt')
        if not os.path.exists(log_file_path) or os.path.getsize(log_file_path) < MAX_LOG_SIZE_BYTES:
            return log_file_path
        log_file_suffix += 1

def handle_message(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    chat_ref = db.collection('active_chats').document(str(user_id))
    chat = chat_ref.get()

    if chat.exists:
        partner_id = chat.to_dict().get('partner')
        timestamp = datetime.now().isoformat()

        log_file_path = get_log_file_path(user_id)

        try:
            # Periksa apakah pesan yang diterima adalah teks
            if update.message.text:
                message_data = f"{timestamp} - {user_id} to {partner_id}: {update.message.text}\n"
                with open(log_file_path, 'a') as log_file:
                    log_file.write(message_data)
                
                context.bot.send_message(chat_id=partner_id, text=update.message.text)
                
                # Upload file log setelah menulis pesan
                upload_log_to_google_drive(log_file_path, '1OQpqIlKPYWSvOTaXqQIOmMW3g1N0sQzf')

            # Periksa apakah pesan yang diterima adalah stiker
            elif update.message.sticker:
                sticker = update.message.sticker
                if sticker:  # Memeriksa apakah sticker tidak None
                    sticker_id = sticker.file_id
                    sticker_file_path = f'/tmp/{user_id}_sticker_{sticker_id}.png'

                    context.bot.send_sticker(chat_id=partner_id, sticker=sticker_id)

                    try:
                        # Get file info and download sticker
                        file_info = context.bot.get_file(sticker_id)
                    
                        # Download file directly
                        file_info.download(sticker_file_path)
                    
                        # Upload sticker to Google Drive
                        upload_log_to_google_drive(sticker_file_path, '1KbEpuvg0rKDJSD76oPDi_RFecEcPxFE6')

                    except Exception as e:
                        logging.error(f"An error occurred while handling sticker: {e}")
                    finally:
                        # Remove local file after upload
                        if os.path.exists(sticker_file_path):
                            os.remove(sticker_file_path)
                            logging.info(f'Removed local file {sticker_file_path}')

        except Exception as e:
            logging.error(f"Error handling message: {e}")
            context.bot.send_message(chat_id=user_id, text="Terjadi kesalahan saat memproses pesan.")
    else:
        context.bot.send_message(chat_id=user_id, text="Anda belum terhubung dengan pasangan.")



def handle_photo(update: Update, context: CallbackContext):
    photo = update.message.photo[-1]  # Ambil foto dengan resolusi tertinggi
    file = context.bot.get_file(photo.file_id)
    file.download('photo.jpg')  # Simpan foto
    context.bot.send_message(chat_id=update.effective_chat.id,
                             text="Foto diterima!")


def handle_voice_note(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    voice = update.message.voice

    # Dapatkan file_id dari voice note
    file_id = voice.file_id

    # Ambil partner_id dari Firestore
    chat_ref = db.collection('active_chats').document(str(user_id))
    chat = chat_ref.get()
    if chat.exists:
        partner_id = chat.to_dict().get('partner')

        try:
            # Kirimkan voice note ke partner
            context.bot.send_voice(chat_id=partner_id, voice=file_id)
            
            # Unduh file dari Telegram
            file = context.bot.get_file(file_id)
            unique_timestamp = generate_unique_timestamp()
            filename = f'voice_note_{unique_timestamp}.ogg'
            file.download(filename)  # Simpan dengan nama file unik

            # Upload file ke Firebase Storage
            blob = bucket.blob(f'voice_notes/{filename}')
            blob.upload_from_filename(filename)
            
            # Hapus file sementara setelah diupload
            os.remove(filename)

        except Exception as e:
            logging.error(f"Failed to send voice note: {e}")
            # Kirim pesan ke pengguna hanya jika ada masalah
            context.bot.send_message(chat_id=user_id, text="Gagal mengirim voice note.")
    else:
        context.bot.send_message(chat_id=user_id, text="Anda belum terhubung dengan pasangan.")


def handle_location(update: Update, context: CallbackContext):
    location = update.message.location
    user_id = update.message.from_user.id

    # Generate Google Maps URL
    maps_url = f"https://www.google.com/maps?q={location.latitude},{location.longitude}"

    # Retrieve partner_id from Firestore
    chat_ref = db.collection('active_chats').document(str(user_id))
    chat = chat_ref.get()

    if chat.exists:
        partner_id = chat.to_dict().get('partner')
        try:
            # Send location to partner
            context.bot.send_location(
                chat_id=partner_id,
                latitude=location.latitude,
                longitude=location.longitude
            )

            # Send Google Maps URL to the user and partner
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Lokasi diterima dan dikirim ke pasangan! Latitude: {location.latitude}, Longitude: {location.longitude}\n\n[Google Maps Link]({maps_url})"
            )

            # Optionally, send the Google Maps URL to the partner as well
            context.bot.send_message(
                chat_id=partner_id,
                text=f"Anda telah menerima lokasi!\n\n[Google Maps Link]({maps_url})"
            )

            # Save Google Maps URL to Firestore
            timestamp = generate_unique_timestamp()
            message_data = {
                'sender_id': user_id,
                'recipient_id': partner_id,
                'type': 'location',
                'content': maps_url,
                'timestamp': timestamp
            }
            db.collection('messages').document(timestamp).set(message_data)
            logging.info("Location URL saved to Firestore.")

        except Exception as e:
            logging.error(f"Failed to handle location: {e}")
            context.bot.send_message(
                chat_id=user_id,
                text="Gagal memproses lokasi.")
    else:
        context.bot.send_message(
            chat_id=user_id,
            text="Anda belum terhubung dengan pasangan.")




import urllib.parse


def get_user_info(user_id: str):
    user_ref = db.collection('users').document(user_id)
    user_doc = user_ref.get()

    if user_doc.exists:
        user_data = user_doc.to_dict()
        username = user_data.get('username', 'Tidak ada username')
        photo_id = user_data.get('photo', None)  # Assume photo ID is stored
        return username, photo_id
    else:
        return None, None


def user_info(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    username, photo_id = get_user_info(str(user_id))

    response_text = f"User ID: {user_id}\nUsername: {username}\n"

    profile_photos = context.bot.get_user_profile_photos(user_id)
    if profile_photos.total_count > 0:
        photo_file_id = profile_photos.photos[0][-1].file_id
        context.bot.send_photo(chat_id=user_id, photo=photo_file_id)
        context.bot.send_message(chat_id=user_id,
                                 text=response_text,
                                 parse_mode='Markdown')
    else:
        response_text += "Foto Profil: Tidak tersedia."
        context.bot.send_message(chat_id=user_id,
                                 text=response_text,
                                 parse_mode='Markdown')

def partner_info(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    # Get the active chat for the user
    chat_ref = db.collection('active_chats').document(str(user_id))
    chat_doc = chat_ref.get()

    if not chat_doc.exists:
        context.bot.send_message(chat_id=user_id,
                                 text="Anda tidak sedang dalam chat.")
        return

    partner_id = chat_doc.to_dict().get('partner')

    # Retrieve partner's information
    partner_ref = db.collection('users').document(str(partner_id))
    partner_doc = partner_ref.get()

    if not partner_doc.exists:
        context.bot.send_message(
            chat_id=user_id,
            text="Informasi tentang pasangan tidak ditemukan.")
        return

    partner_data = partner_doc.to_dict()
    partner_username = partner_data.get('username', 'Tidak ada username')
    partner_photo_id = partner_data.get('photo', None)

    response_text = f"User ID: {partner_id}\nUsername: {partner_username}\n"

    profile_photos = context.bot.get_user_profile_photos(partner_id)
    if profile_photos.total_count > 0:
        photo_file_id = profile_photos.photos[0][-1].file_id
        context.bot.send_photo(chat_id=user_id, photo=photo_file_id)
        context.bot.send_message(chat_id=user_id,
                                 text=response_text,
                                 parse_mode='Markdown')
    else:
        response_text += "Foto Profil: Tidak tersedia."
        context.bot.send_message(chat_id=user_id,
                                 text=response_text,
                                 parse_mode='Markdown')



def broadcast(update: Update, context: CallbackContext):
    # List of admin IDs
    admin_ids = [2082265412, 6069719700]

    # Get the user ID of the person who issued the command
    user_id = update.message.from_user.id

    # Check if the user is an admin
    if user_id not in admin_ids:
        context.bot.send_message(
            chat_id=user_id,
            text="You are not authorized to use this command.")
        return

    # Check if a message is provided
    if len(context.args) == 0:
        context.bot.send_message(chat_id=user_id,
                                 text="Please provide a message to broadcast.")
        return

    broadcast_message = ' '.join(context.args)

    # URL gambar profil bot atau gambar yang ingin dikirim
    # Gunakan URL gambar atau ID file gambar yang diupload
    bot_profile_photo_url = 'https://upload.wikimedia.org/wikipedia/id/6/6a/Prof_Martono_UNNES.png'  # Ganti dengan URL gambar yang sesuai

    # Get all user IDs from Firestore
    users_ref = db.collection('users')
    users = users_ref.stream()

    for user in users:
        recipient_id = user.id
        try:
            # Send the photo with caption
            context.bot.send_photo(
                chat_id=recipient_id,
                photo=bot_profile_photo_url,
                caption=broadcast_message
            )
        except Exception as e:
            logging.error(f"Failed to send broadcast to {recipient_id}: {e}")

    context.bot.send_message(chat_id=user_id,
                             text="Broadcast message and photo sent to all users.")



def list_banned(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    # Retrieve the list of banned users
    banned_users_ref = db.collection('banned_users')
    banned_users_docs = banned_users_ref.stream()

    if not banned_users_docs:
        context.bot.send_message(chat_id=user_id,
                                 text="No banned users found.")
        return

    banned_list = []
    for doc in banned_users_docs:
        banned_list.append(doc.id)

    banned_list_text = '\n'.join(banned_list)
    context.bot.send_message(chat_id=user_id,
                             text=f"Banned Users:\n{banned_list_text}")


    context.bot.send_message(chat_id=user_id,
                             text="You are not authorized to use this command.")

    if len(context.args) != 0:
        # Assuming the target user_id is provided as an argument
        target_id = context.args[0]

        # Check if the target user exists
        target_ref = db.collection('users').document(target_id)
        target_doc = target_ref.get()

        if not target_doc.exists:
            context.bot.send_message(chat_id=user_id,
                                     text="The user ID does not exist.")
            return

        # Move the target user to the banned_users collection
        banned_user_ref = db.collection('banned_users').document(target_id)
        banned_user_ref.set(target_doc.to_dict())

        # Delete the target user from users collection
        target_ref.delete()

        # Remove the target user from any active chat if exists
        db.collection('active_chats').document(target_id).delete()

        context.bot.send_message(chat_id=user_id,
                                 text=f"User {target_id} has been banned.")
    else:
        context.bot.send_message(chat_id=user_id, text="Please provide a user ID to ban.")
        target_doc = target_ref.get()

        if not target_doc.exists:
            context.bot.send_message(chat_id=user_id,
                                     text="The user ID does not exist.")
            



def banned_user(update: Update, context: CallbackContext):
    # List of admin IDs
    admin_ids = [2082265412, 6069719700]

    user_id = update.message.from_user.id

    # Check if the user is an admin
    if user_id not in admin_ids:
        context.bot.send_message(
            chat_id=user_id,
            text="You are not authorized to use this command.")
        return

    # Check if an argument is provided
    if len(context.args) == 0:
        context.bot.send_message(chat_id=user_id, text="Please provide a user ID to ban.")
        return

    # Try to convert the provided argument to an integer (if the user ID is numeric)
    try:
        target_id = str(context.args[0])  # Jika user ID numeric, bisa gunakan int(context.args[0])
    except ValueError:
        context.bot.send_message(chat_id=user_id, text="Invalid user ID format.")
        return

    # Check if the target user exists
    target_ref = db.collection('users').document(target_id)
    target_doc = target_ref.get()

    if not target_doc.exists:
        context.bot.send_message(chat_id=user_id,
                                 text="The user ID does not exist.")
        return

    # Move the target user to the banned_users collection
    banned_user_ref = db.collection('banned_users').document(target_id)
    banned_user_ref.set(target_doc.to_dict())

    # Delete the target user from users collection
    target_ref.delete()

    # Remove the target user from any active chat if exists
    db.collection('active_chats').document(target_id).delete()

    context.bot.send_message(chat_id=user_id,
                             text=f"User {target_id} has been banned successfully.")




def unbanned_user(update: Update, context: CallbackContext):
    # List of admin IDs
    admin_ids = [2082265412, 6069719700]

    user_id = update.message.from_user.id

    # Check if the user is an admin
    if user_id not in admin_ids:
        context.bot.send_message(
            chat_id=user_id,
            text="You are not authorized to use this command.")
        return

    if len(context.args) != 1:
        context.bot.send_message(chat_id=user_id,
                                 text="Please provide the user ID to unban.")
        return

    unbanned_user_id = context.args[0]

    # Check if the user is in the banned_users collection
    banned_user_ref = db.collection('banned_users').document(unbanned_user_id)
    banned_user_doc = banned_user_ref.get()

    if not banned_user_doc.exists:
        context.bot.send_message(chat_id=user_id,
                                 text="User ID not found in banned list.")
        return

    # Move user back to users collection
    db.collection('users').document(unbanned_user_id).set(
        banned_user_doc.to_dict())

    # Delete from banned_users collection
    banned_user_ref.delete()

    context.bot.send_message(
        chat_id=user_id, text=f"User {unbanned_user_id} has been unbanned.")



# List of admin IDs
admin_ids = [2082265412, 6069719700]  # Ganti dengan ID admin yang sesuai

def lapor_admin(update: Update, context: CallbackContext):
    # Check if the command is /lapor_admin
    if update.message.text.startswith('/lapor_admin'):
        user_id = update.message.from_user.id
        chat_id = update.message.chat_id

        # Extract arguments from command
        args = context.args
        if len(args) < 1:
            context.bot.send_message(
                chat_id=chat_id,
                text="Format perintah salah. \n Gunakan: /lapor_admin <report_text>"
            )
            return

        report_text = ' '.join(args) if len(args) > 0 else "Laporan tanpa teks."
        
        # Get the partner ID from Firestore
        try:
            chat_ref = db.collection('active_chats').document(str(user_id))
            chat = chat_ref.get()
            if chat.exists:
                partner_id = chat.to_dict().get('partner')
            else:
                context.bot.send_message(chat_id=chat_id, text="Anda belum terhubung dengan pasangan.")
                return
        except Exception as e:
            logging.error(f"Error accessing Firestore: {e}")
            context.bot.send_message(chat_id=chat_id, text="Terjadi kesalahan saat memproses laporan.")
            return

        for admin_id in admin_ids:
            try:
                # Send text only to each admin
                context.bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"ID Pelapor: {user_id}\n"
                        f"ID Terlapor: {partner_id}\n"
                        f"Pesan: {report_text}"
                    )
                )
            except Exception as e:
                logging.error(f"Failed to send report to admin {admin_id}: {e}")

        # Notify the user that the report was sent
        context.bot.send_message(
            chat_id=chat_id,
            text="Laporan Anda telah dikirim ke admin."
        )

        # Optionally, return to the active chat
        context.bot.send_message(
            chat_id=chat_id,
            text="Kembali ke chat aktif."
        )



def button(update: Update, context: CallbackContext):
    query = update.callback_query
    user_id = query.from_user.id
    command = query.data

    if command == 'search':
        search(update, context)
    elif command == 'next':
        next_chat(update, context)
    elif command == 'stop':
        stop_chat(update, context)

    query.answer()  # Acknowledge the callback query



def main():
    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    # Tambahkan handler untuk perintah
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("search", search))
    dp.add_handler(CommandHandler("stop", stop_chat))
    dp.add_handler(CommandHandler("next", next_chat))
    dp.add_handler(CommandHandler("userinfo", user_info))
    dp.add_handler(CommandHandler("partnerinfo", partner_info))
    dp.add_handler(CommandHandler("broadcast", broadcast))
    dp.add_handler(CommandHandler("banned_user", banned_user))
    dp.add_handler(CommandHandler("unbanned_user", unbanned_user))
    dp.add_handler(CommandHandler("list_banned", list_banned))


   
    # Add handler for text messages that are not commands
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command & ~Filters.regex('^/lapor_admin'), handle_message))
    dp.add_handler(CommandHandler("lapor_admin", lapor_admin))

  

    # dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    dp.add_handler(MessageHandler(Filters.sticker, handle_message))
    dp.add_handler(MessageHandler(Filters.photo, handle_photo))
    dp.add_handler(MessageHandler(Filters.voice, handle_voice_note))
    dp.add_handler(MessageHandler(Filters.location, handle_location))



    # Tambahkan handler untuk tombol inline
    dp.add_handler(CallbackQueryHandler(button))

    updater.start_polling()
    updater.idle()



if __name__ == '__main__':
    main()
