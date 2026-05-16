import argparse
import os
import sys
import requests
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import count
from typing import Any

API_URL = "<YOUR_PIWIGO_URL>/ws.php?format=json"
USERNAME = os.getenv("PIWIGO_USER", "<YOUR_USERNAME>")
PASSWORD = os.getenv("PIWIGO_PASSWORD", "<YOUR_PASSWORD>")

def login(session: requests.Session) -> None:
    payload = {
        "method": "pwg.session.login",
        "username": USERNAME,
        "password": PASSWORD,
    }
    try:
        response = session.post(API_URL, data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get("stat") != "ok":
            raise RuntimeError(f"Login failed: {data}")
        print("Login successful.")
    except requests.exceptions.RequestException as e:
        # raise RuntimeError(f"Login request failed: {e}")
        print(f"Login request failed: {e}")
        exit(1)
    except ValueError as e:
        #raise RuntimeError(f"Invalid JSON response: {e}")
        print(f"Invalid JSON response: {e}")
        exit(1)

def get_albums(session: requests.Session) -> list[dict[str, Any]]:
    payload = {
        "method": "pwg.categories.getList",
        "recursive": "1",
        "order": "rank",
    }
    response = session.post(API_URL, data=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("stat") != "ok":
        raise RuntimeError(f"Failed to get albums: {data}")
    return data.get("result", {}).get("categories", [])

def build_album_tree(categories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes: dict[int, dict[str, Any]] = {}
    roots: list[dict[str, Any]] = []

    for cat in categories:
        cat_id = int(cat.get("id", 0))
        nodes[cat_id] = {
            "id": cat_id,
            "name": cat.get("name", "(unnamed)"),
            "parent": int(cat.get("id_uppercat", 0) or 0),
            "children": [],
        }

    for node in nodes.values():
        parent_id = node["parent"]
        if parent_id and parent_id in nodes:
            nodes[parent_id]["children"].append(node)
        else:
            roots.append(node)

    return roots

def build_number_to_id_map(nodes: list[dict[str, Any]], counter: count | None = None, mapping: dict[int, int] | None = None) -> dict[int, int]:
    if counter is None:
        counter = count(1)
    if mapping is None:
        mapping = {}

    for node in nodes:
        album_number = next(counter)
        mapping[album_number] = node["id"]
        build_number_to_id_map(node["children"], counter, mapping)

    return mapping

def get_pwg_token(session: requests.Session) -> str:
    payload = {
        "method": "pwg.session.getStatus",
    }
    response = session.post(API_URL, data=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("stat") != "ok":
        raise RuntimeError(f"Failed to get session token: {data}")
    token = data.get("result", {}).get("pwg_token")
    if not token:
        raise RuntimeError("Failed to retrieve pwg_token")
    return token


def move_album(session: requests.Session, category_id: int, parent_id: int, pwg_token: str) -> None:
    payload = {
        "method": "pwg.categories.move",
        "category_id": str(category_id),
        "parent": str(parent_id),
        "pwg_token": pwg_token,
    }
    response = session.post(API_URL, data=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("stat") != "ok":
        raise RuntimeError(f"Failed to move album: {data}")


def upload_image(session: requests.Session, category_id: int, image_path: str, pwg_token: str) -> None:
    """Upload a single image to the specified category."""
    with open(image_path, "rb") as image_file:
        files = {"image": image_file}
        payload = {
            "method": "pwg.images.addSimple",
            "category": str(category_id),
            "pwg_token": pwg_token,
            "author": USERNAME,
            "name": os.path.splitext(os.path.basename(image_path))[0],
        }
        response = session.post(API_URL, files=files, data=payload, timeout=30)
        response.raise_for_status()
        try:
            data = response.json()
            completion_payload = {
            "method": "pwg.images.uploadCompleted",
            "image_id": str(data['result']['image_id']),
            "category_id": str(category_id),
            "pwg_token": pwg_token,
            }
            completion_response = session.post(API_URL, data=completion_payload, timeout=15)
            completion_response.raise_for_status()

        except requests.exceptions.JSONDecodeError:
            print(f"Error: Response is not valid JSON")
            print(f"Status: {response.status_code}")
            print(f"Response text: {response.text[:500]}")
            raise RuntimeError(f"Invalid JSON response from API for image {image_path}")
        if data.get("stat") != "ok":
            raise RuntimeError(f"Failed to upload image {image_path}: {data}")
        
        print(f"Uploaded: {os.path.basename(image_path)}")


def upload_folder(session: requests.Session, category_id: int, folder_path: str) -> None:
    """Upload all images from the specified folder to the category."""
    if not os.path.isdir(folder_path):
        raise ValueError(f"Folder {folder_path} does not exist or is not a directory")
    
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'}
    uploaded_count = 0
    failed_count = 0

    for filename in sorted(os.listdir(folder_path)):
        if os.path.splitext(filename)[1].lower() in image_extensions:
            image_path = os.path.join(folder_path, filename)
            for attempt in range(1, 4):
                try:
                    if attempt == 1:
                        pwg_token = get_pwg_token(session)
                    else:
                        pwg_token = get_pwg_token(session)
                    upload_image(session, category_id, image_path, pwg_token)
                    uploaded_count += 1
                    break
                except Exception as e:
                    if attempt < 3:
                        print(f"Upload failed for {filename}, retrying ({attempt}/3): {e}")
                    else:
                        print(f"Unable to upload {filename} after 3 attempts: {e}")
                        failed_count += 1

    if uploaded_count > 0:
        print("Finalizing upload...")

    print(f"\nUpload complete: {uploaded_count} image(s) uploaded to album {category_id}")
    if failed_count > 0:
        print(f"{failed_count} image(s) failed to upload.")


def get_images(session: requests.Session, category_id: int) -> list[dict[str, Any]]:
    payload = {
        "method": "pwg.categories.getImages",
        "cat_id": str(category_id),
        "per_page": "100",  # Adjust if needed
    }
    response = session.post(API_URL, data=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("stat") != "ok":
        raise RuntimeError(f"Failed to get images: {data}")
    return data.get("result", {}).get("images", [])


def get_all_images(session: requests.Session, nodes: list[dict[str, Any]], max_workers: int = 8) -> list[dict[str, Any]]:
    """Get all images from all albums recursively."""
    def collect_category_ids(categories: list[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        for cat in categories:
            ids.append(cat["id"])
            ids.extend(collect_category_ids(cat["children"]))
        return ids

    category_ids = collect_category_ids(nodes)
    if not category_ids:
        return []

    images: list[dict[str, Any]] = []
    worker_count = min(max_workers, len(category_ids))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(get_images, session, category_id): category_id for category_id in category_ids}
        for future in as_completed(futures):
            try:
                images.extend(future.result())
            except Exception:
                pass  # Skip albums where we can't fetch images

    return images


def get_image_counts(session: requests.Session, nodes: list[dict[str, Any]], max_workers: int = 8) -> dict[int, int]:
    """Get image count for each album efficiently using parallel requests."""
    def collect_category_ids(categories: list[dict[str, Any]]) -> list[int]:
        ids: list[int] = []
        for cat in categories:
            ids.append(cat["id"])
            ids.extend(collect_category_ids(cat["children"]))
        return ids

    category_ids = collect_category_ids(nodes)
    if not category_ids:
        return {}

    image_counts: dict[int, int] = {}
    worker_count = min(max_workers, len(category_ids))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(get_images, session, category_id): category_id for category_id in category_ids}
        for future in as_completed(futures):
            try:
                category_id = futures[future]
                images = future.result()
                image_counts[category_id] = len(images)
            except Exception:
                pass  # Skip albums where we can't fetch images

    return image_counts


def download_image(session: requests.Session, image_url: str, filename: str, folder: str) -> None:
    """Download a single image to the specified folder."""
    os.makedirs(folder, exist_ok=True)
    filepath = os.path.join(folder, filename)
    
    response = session.get(image_url, timeout=30)
    response.raise_for_status()
    
    with open(filepath, "wb") as f:
        f.write(response.content)
    print(f"Downloaded: {filename}")


def download_album_images(session: requests.Session, category_id: int, folder: str, limit: int | None = None) -> None:
    """Download images from the specified category to the folder."""
    images = get_images(session, category_id)
    if not images:
        print("No images found in this album.")
        return
    
    # Sort images by date_available (newest first) and limit if specified
    if limit is not None:
        images = sorted(images, key=lambda x: x.get("date_available", ""), reverse=True)[:limit]
    
    downloaded_count = 0
    for image in images:
        image_url = image.get("element_url")  # Full-size image URL
        if not image_url:
            continue
        # Use the 'file' field for filename, fallback to URL basename
        filename = image.get("file") or os.path.basename(image_url)
        
        download_image(session, image_url, filename, folder)
        downloaded_count += 1
    
    print(f"\nDownload complete: {downloaded_count} image(s) downloaded to {folder}")


def create_album(session: requests.Session, parent_id: int, name: str) -> int:
    payload = {
        "method": "pwg.categories.add",
        "name": name,
    }
    try:
        response = session.post(API_URL, data=payload, timeout=15)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Failed to create album '{name}': {e}")
    except ValueError as e:
        raise RuntimeError(f"Invalid JSON response while creating album '{name}': {e}")

    if data.get("stat") != "ok":
        raise RuntimeError(f"Failed to create album '{name}': {data}")

    category_id = int(data.get("result", {}).get("id", 0) or data.get("result", {}).get("category_id", 0) or 0)
    if category_id == 0:
        raise RuntimeError(f"Failed to parse new album id for '{name}': {data}")

    if parent_id:
        pwg_token = get_pwg_token(session)
        move_album(session, category_id, parent_id, pwg_token)

    return category_id

def get_album_number(album_id: int, nodes: list[dict[str, Any]]) -> int | None:
    """Find the album number for a given album ID."""
    mapping = build_number_to_id_map(nodes)
    id_to_number = {v: k for k, v in mapping.items()}
    return id_to_number.get(album_id)


def print_album_tree(nodes: list[dict[str, Any]], session: requests.Session, depth: int = 0, owner_name: str | None = None, counter: count | None = None, filter_name: str | None = None, number_mapping: dict[int, int] | None = None, image_counts: dict[int, int] | None = None) -> None:
    if counter is None:
        counter = count(1)

    # Create reverse mapping: album_id -> number
    id_to_number = {v: k for k, v in (number_mapping or {}).items()}

    prefix = "  " * depth
    for node in nodes:
        display_name = node["name"]
        if owner_name and depth >= 2:
            display_name = f"{display_name} ({owner_name})"

        # Check if this album matches the filter
        if filter_name and filter_name.lower() not in display_name.lower():
            # If it doesn't match, still process children but don't print this node
            next_owner = owner_name
            if depth == 0:
                next_owner = None
            elif depth == 1:
                next_owner = node["name"]
            print_album_tree(node["children"], session, depth + 1, next_owner, counter, filter_name, number_mapping, image_counts)
            continue

        # Use the pre-computed number from mapping, or generate one if no mapping provided
        if number_mapping is not None:
            album_number = id_to_number.get(node["id"], next(counter))
        else:
            album_number = next(counter)
        
        # Get the number of pictures from pre-computed counts, or fetch if not available
        if image_counts is not None:
            num_pictures = image_counts.get(node["id"], 0)
        else:
            try:
                images = get_images(session, node["id"])
                num_pictures = len(images)
            except Exception:
                num_pictures = 0
        
        print(f"{prefix}- {album_number}. {display_name} ({num_pictures} pictures)")

        next_owner = owner_name
        if depth == 0:
            next_owner = None
        elif depth == 1:
            next_owner = node["name"]

        print_album_tree(node["children"], session, depth + 1, next_owner, counter, filter_name, number_mapping, image_counts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Piwigo album utility")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Display the hierarchical album list",
    )
    parser.add_argument(
        "--add_album",
        nargs="+",
        metavar=("ALBUM_NUMBER", "NAME"),
        help="Create a child album under the specified numbered parent",
    )
    parser.add_argument(
        "--upload",
        nargs=2,
        metavar=("ALBUM_NUMBER", "FOLDER"),
        help="Upload all images from FOLDER to the specified album number",
    )
    parser.add_argument(
        "--download",
        metavar="ALBUM_NUMBER",
        type=int,
        help="Download images from the specified album number to ./pictures",
    )
    parser.add_argument(
        "--last",
        metavar="NB",
        type=int,
        help="When used with --download, download only the latest NB pictures",
    )
    parser.add_argument(
        "--find",
        metavar="NAME",
        help="When used with --list, filter albums containing the specified name",
    )
    parser.add_argument(
        "--recent",
        metavar="NB",
        type=int,
        help="Download the latest NB pictures added across all albums to ./pictures",
    )
    return parser.parse_args()


def confirm_action(prompt: str) -> bool:
    reply = input(f"{prompt} [y/N]: ").strip().lower()
    return reply == "y" or reply == "yes"


def main() -> None:
    args = parse_args()
    
    # Load configuration from conf.yml
    try:
        with open('conf.yml', 'r') as f:
            config = yaml.safe_load(f) or {}
        global API_URL, USERNAME, PASSWORD
        API_URL = config.get('API_URL', API_URL)
        USERNAME = config.get('USERNAME', USERNAME)
        PASSWORD = config.get('PASSWORD', PASSWORD)
    except FileNotFoundError:
        print("conf.yml not found, using default values.")
    except yaml.YAMLError as e:
        print(f"Error parsing conf.yml: {e}, using default values.")
    
    # Check for argument conflicts
    if args.last is not None and args.download is None:
        raise ValueError("--last can only be used with --download")
    if args.find is not None and not args.list:
        raise ValueError("--find can only be used with --list")
    
    show_list = args.list or not any([args.add_album, args.upload, args.download, args.recent])

    try:
        with requests.Session() as session:
            login(session)
            if args.add_album:
                if len(args.add_album) < 2:
                    raise ValueError("--add_album requires a parent number and a name")
                parent_number = int(args.add_album[0])
                child_name = " ".join(args.add_album[1:]).strip()
                if not child_name:
                    raise ValueError("Album name cannot be empty")

                albums = get_albums(session)
                tree = build_album_tree(albums)
                mapping = build_number_to_id_map(tree)
                if parent_number not in mapping:
                    raise ValueError(f"Album number {parent_number} is not valid")

                parent_id = mapping[parent_number]
                parent_name = next(
                    (cat["name"] for cat in albums if int(cat.get("id", 0)) == parent_id),
                    None,
                )
                description = f"Create album '{child_name}' under parent {parent_number}"
                if parent_name:
                    description += f" ({parent_name})"
                description += f"?"

                if not confirm_action(description):
                    print("Operation cancelled.")
                    return

                new_id = create_album(session, parent_id, child_name)
                
                # Rebuild the tree to find the number of the newly created album
                albums = get_albums(session)
                tree = build_album_tree(albums)
                new_album_number = get_album_number(new_id, tree)
                
                if parent_name:
                    if new_album_number:
                        print(f"Created album '{child_name}' under parent {parent_number} ({parent_name}) - new album number: {new_album_number}")
                    else:
                        print(f"Created album '{child_name}' under parent {parent_number} ({parent_name}) failed. Do it in Piwigo.")
                else:
                    if new_album_number:
                        print(f"Created album '{child_name}' under parent {parent_number} (id={parent_id}), new id={new_id}, new album number: {new_album_number}")
                    else:
                        print(f"Created album '{child_name}' under parent {parent_number} (id={parent_id}), new id={new_id}  Do it in Piwigo.")
            elif args.upload:
                album_number = int(args.upload[0])
                folder_path = args.upload[1]

                albums = get_albums(session)
                tree = build_album_tree(albums)
                mapping = build_number_to_id_map(tree)
                if album_number not in mapping:
                    raise ValueError(f"Album number {album_number} is not valid")

                album_id = mapping[album_number]
                album_name = next(
                    (cat["name"] for cat in albums if int(cat.get("id", 0)) == album_id),
                    None,
                )
                description = f"Upload images to album {album_number}"
                if album_name:
                    description += f" ({album_name})"
                description += f"?"

                if not confirm_action(description):
                    print("Operation cancelled.")
                    return

                upload_folder(session, album_id, folder_path)
            elif args.download is not None:
                album_number = args.download
                limit = args.last

                albums = get_albums(session)
                tree = build_album_tree(albums)
                mapping = build_number_to_id_map(tree)
                if album_number not in mapping:
                    raise ValueError(f"Album number {album_number} is not valid")

                album_id = mapping[album_number]
                album_name = next(
                    (cat["name"] for cat in albums if int(cat.get("id", 0)) == album_id),
                    None,
                )
                description = f"Download "
                if limit is not None:
                    description += f"the latest {limit} images "
                else:
                    description += "all images "
                description += f"from album {album_number}"
                if album_name:
                    description += f" ({album_name})"
                description += f" to ./pictures?"

                if not confirm_action(description):
                    print("Operation cancelled.")
                    return

                download_album_images(session, album_id, "./pictures", limit)
            elif args.recent is not None:
                limit = args.recent
                
                albums = get_albums(session)
                tree = build_album_tree(albums)
                description = f"Download the latest {limit} pictures added across all albums to ./pictures?"
                
                if not confirm_action(description):
                    print("Operation cancelled.")
                    return
                
                print("Fetching images from all albums...")
                all_images = get_all_images(session, tree)
                if not all_images:
                    print("No images found.")
                    return
                
                # Sort by date_available (newest first) and limit
                sorted_images = sorted(all_images, key=lambda x: x.get("date_available", ""), reverse=True)[:limit]
                
                print("Downloading images...")
                os.makedirs("./pictures", exist_ok=True)
                downloaded_count = 0
                for image in sorted_images:
                    image_url = image.get("element_url")
                    if not image_url:
                        continue
                    filename = image.get("file") or os.path.basename(image_url)
                    try:
                        download_image(session, image_url, filename, "./pictures")
                        downloaded_count += 1
                    except Exception as e:
                        print(f"Failed to download {filename}: {e}")
                
                print(f"\nDownload complete: {downloaded_count} image(s) downloaded to ./pictures")
            elif show_list:
                albums = get_albums(session)
                tree = build_album_tree(albums)
                mapping = build_number_to_id_map(tree)
                
                # Pre-fetch image counts in parallel for faster display
                print("Fetching album information...")
                image_counts = get_image_counts(session, tree)
                
                print("Albums:")
                print_album_tree(tree, session, filter_name=args.find, number_mapping=mapping, image_counts=image_counts)
    except (RuntimeError, ValueError) as e:
        print(e)
        sys.exit(1)

if __name__ == "__main__":
    main()