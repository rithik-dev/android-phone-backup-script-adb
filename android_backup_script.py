#!/usr/bin/env python3
import os
import sys
import subprocess
import datetime
import shutil
import tarfile
from concurrent.futures import ThreadPoolExecutor, as_completed

# List of standard Android user storage directories to back up
TARGET_DIRECTORIES = [
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Documents",
    "/sdcard/Download",
    "/sdcard/Music",
    "/sdcard/Movies",
    "/sdcard/Alarms",
    "/sdcard/Notifications",
    "/sdcard/Ringtones",
    "/sdcard/Podcasts",
    "/sdcard/Android/media"  # Contains app media like WhatsApp or Signal attachments
]

def check_adb_available():
    """Verifies that the ADB executable is accessible via the system PATH."""
    if not shutil.which("adb"):
        print("Error: 'adb' executable not found in your system's PATH.")
        print("Please install Android SDK Platform Tools or add it to your environment variables.")
        sys.exit(1)

def get_connected_devices():
    """Retrieves a list of devices recognized by ADB and their states."""
    try:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True, check=True)
        lines = result.stdout.strip().split("\n")[1:]  # Skip the header line
        devices = []
        for line in lines:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    devices.append((parts[0], parts[1]))
        return devices
    except subprocess.CalledProcessError as e:
        print(f"Error checking connected devices: {e}")
        sys.exit(1)

def verify_device_state():
    """Ensures exactly one authorized device is connected before starting the backup."""
    devices = get_connected_devices()
    
    if not devices:
        print("Error: No devices found. Please plug in your phone and enable USB Debugging.")
        sys.exit(1)
        
    if len(devices) > 1:
        print("Error: Multiple devices connected. Please disconnect other devices or emulators.")
        for dev, state in devices:
            print(f" - {dev} ({state})")
        sys.exit(1)
        
    device_id, state = devices[0]
    if state == "unauthorized":
        print(f"Error: Device [{device_id}] is unauthorized.")
        print("Please check your phone's screen and allow the USB Debugging permission prompt.")
        sys.exit(1)
        
    if state != "device":
        print(f"Error: Device [{device_id}] is in an unexpected state: '{state}'.")
        sys.exit(1)
        
    print(f"Successfully connected to authorized device: {device_id}")
    return device_id

def pull_directory(remote_dir, destination_root):
    """Pulls a single directory, choosing an optimized TAR-archive method for file-heavy paths."""
    folder_name = os.path.basename(remote_dir.rstrip("/"))
    
    # Check if the folder exists on the phone via an adb shell check
    check_cmd = ["adb", "shell", f"[ -d '{remote_dir}' ] && echo 'exists' || echo 'missing'"]
    check_result = subprocess.run(check_cmd, capture_output=True, text=True)
    
    if "missing" in check_result.stdout:
        return remote_dir, "Skipped (Directory missing on device)"
        
    # Dynamically query file count inside this directory to determine optimization strategy
    count_cmd = ["adb", "shell", f"find '{remote_dir}' -type f 2>/dev/null | wc -l"]
    count_result = subprocess.run(count_cmd, capture_output=True, text=True)
    
    try:
        file_count = int(count_result.stdout.strip())
    except ValueError:
        file_count = 0

    # Optimization threshold: If folder contains more than 200 files, archive it on-device first.
    # This prevents the severe per-file ADB handshake latency bottleneck.
    if file_count > 200:
        tar_filename = f"speed_backup_{folder_name}.tar"
        remote_tar_path = f"/sdcard/{tar_filename}"
        local_tar_path = os.path.join(destination_root, tar_filename)
        
        # 1. Bundle files into a single continuous tar archive stream directly on the phone
        parent_dir = os.path.dirname(remote_dir)
        tar_cmd = ["adb", "shell", f"tar -cf {remote_tar_path} -C {parent_dir} {folder_name}"]
        tar_process = subprocess.run(tar_cmd, capture_output=True, text=True)
        
        if tar_process.returncode == 0:
            # 2. Transfer the single large consolidated file (massively faster than thousands of files)
            pull_cmd = ["adb", "pull", remote_tar_path, local_tar_path]
            pull_process = subprocess.run(pull_cmd, capture_output=True, text=True)
            
            # 3. Always clean up the temporary archive from the phone to free space
            subprocess.run(["adb", "shell", f"rm {remote_tar_path}"], capture_output=True)
            
            if pull_process.returncode == 0:
                # 4. Extract archive locally using Python's native tarfile library
                try:
                    with tarfile.open(local_tar_path, "r:") as tar:
                        tar.extractall(path=destination_root)
                    os.remove(local_tar_path)  # Delete local temp archive
                    return remote_dir, f"Completed successfully (Optimized TAR path for {file_count} files)"
                except Exception as e:
                    return remote_dir, f"Failed local extraction: {e}"
        # Fallback to standard pull if on-device tar fails due to storage constraints
    
    # Standard single-stream pull execution for directories with low file density
    pull_cmd = ["adb", "pull", "-a", remote_dir, destination_root]
    process = subprocess.run(pull_cmd, capture_output=True, text=True)
    
    if process.returncode == 0:
        return remote_dir, "Completed successfully (Standard pull)"
    else:
        error_msg = process.stderr.strip().replace('\n', ' ') if process.stderr else "Unknown error"
        return remote_dir, f"Failed (Code {process.returncode}): {error_msg}"

def perform_parallel_backup(destination_root, max_workers=3):
    """Orchestrates parallel pulls using a concurrent ThreadPoolExecutor framework."""
    print("\n" + "="*60)
    print(f"Starting Multithreaded Android Storage Backup: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Destination Folder: {destination_root}")
    print(f"Parallel Worker Threads: {max_workers}")
    print("="*60 + "\n")
    
    results = {}
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_dir = {
            executor.submit(pull_directory, remote_dir, destination_root): remote_dir 
            for remote_dir in TARGET_DIRECTORIES
        }
        
        for future in as_completed(future_to_dir):
            remote_dir = future_to_dir[future]
            try:
                remote_dir, status = future.result()
                print(f"[Thread Monitor] Finished processing: {remote_dir} -> {status}")
                results[remote_dir] = status
            except Exception as exc:
                print(f"[Thread Monitor] {remote_dir} generated an internal thread exception: {exc}")
                results[remote_dir] = f"Exception: {exc}"
                
    print("\n" + "="*60)
    print("Final Storage Backup Summary Report:")
    print("="*60)
    success_count = 0
    skipped_count = 0
    failed_count = 0
    
    for remote_dir in TARGET_DIRECTORIES:
        status = results.get(remote_dir, "Unknown execution status")
        print(f" - {remote_dir:<25} : {status}")
        if "Completed" in status:
            success_count += 1
        elif "Skipped" in status:
            skipped_count += 1
        else:
            failed_count += 1
            
    print("-"*60)
    print(f"Completed Tasks: {success_count} | Skipped Paths: {skipped_count} | Failed Sub-processes: {failed_count}")
    print(f"Target Working Root Directory: {destination_root}")
    print("="*60)

if __name__ == "__main__":
    check_adb_available()
    verify_device_state()
    
    # Generate the target backup folder name tagged with current datetime framework
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_folder_name = f"Android_Backup_{timestamp}"
    
    # Construct the explicit absolute target path pointing directly inside your Mac Downloads environment
    home_dir = os.path.expanduser("~")
    target_backup_path = os.path.join(home_dir, "Downloads", "Android Backups", backup_folder_name)
    
    # Safely compile the nested tree directory folders cleanly before beginning transfer pipeline
    os.makedirs(target_backup_path, exist_ok=True)
    
    perform_parallel_backup(target_backup_path, max_workers=3)
