# Docker Host Setup (friend's laptop, "Laptop A")

The friend's laptop runs the heavy part of TenantGuard in Docker: gateway, 9 workers, spare, quarantine and brain. Our laptop ("Laptop B") keeps the code, the Claude sessions, the traffic lab and the dashboard, and controls Laptop A's Docker over SSH.

Total time: about 45 minutes, most of it downloads.

---

## Part 1: Install (about 30 min, one time)

1. **GitHub:** sign in as **reki-shu** and accept the invitation to `yashodeepchaudhari/tenantguard` (bell icon → Notifications, or the invite email).
2. **Install Docker Desktop:** <https://www.docker.com/products/docker-desktop/>. During setup keep **"Use WSL 2"** ticked. Restart when asked.
3. **Install Git for Windows:** <https://git-scm.com/download/win> (defaults are fine).
4. **Give Docker enough CPU and RAM.** Open Notepad and save this as `C:\Users\<your-user>\.wslconfig` (set "Save as type: All files"):
   ```
   [wsl2]
   processors=8
   memory=12GB
   ```
   Use your real core count, and leave about 4 GB for Windows. Then in PowerShell run `wsl --shutdown` and start Docker Desktop again.
5. **Test:** open PowerShell and run `docker run hello-world`. It should print "Hello from Docker!".

## Part 2: Network (5 min)

6. Put both laptops on the **same Wi-Fi or phone hotspot**. An Ethernet cable between them is best for the demo.
7. Windows Settings → Network → your Wi-Fi → **Network profile type: Private**.
8. Find your IP: run `ipconfig` and note the **IPv4 Address** (for example `192.168.1.23`).
9. Open the TenantGuard ports. Open **PowerShell as Administrator** and run:
   ```powershell
   New-NetFirewallRule -DisplayName "TenantGuard" -Direction Inbound -Protocol TCP -LocalPort 8000,8200,8300 -Action Allow -Profile Private
   ```

## Part 3: Let the coordinator laptop control Docker (10 min)

This lets us start, stop and rebuild everything from our laptop, so you don't have to type commands during the hackathon.

10. Enable the SSH server. In **PowerShell as Administrator**:
    ```powershell
    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Start-Service sshd
    Set-Service -Name sshd -StartupType Automatic
    ```
11. Allow our laptop's key. Still as Administrator, paste this exactly:
    ```powershell
    $k = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBMc+D4uk3+ZHrdd1U7TaD8c0qhUCfouEeY0oP69MmhY tenantguard-coordinator"
    Add-Content -Path "C:\ProgramData\ssh\administrators_authorized_keys" -Value $k
    icacls.exe "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
    ```
    (This is for an administrator account, which most personal laptops use. If your account is not an admin, put the key line in `C:\Users\<you>\.ssh\authorized_keys` instead.)
12. Stop the laptop from sleeping while plugged in:
    ```powershell
    powercfg /change standby-timeout-ac 0
    ```
13. **Send us these 4 things:**
    - your Windows username (run `whoami`, and send the part after `\`)
    - the IPv4 address from step 8
    - CPU cores and RAM (Task Manager → Performance)
    - "done", once `docker run hello-world` works

## Part 4: Optional, run it yourself without us
```powershell
git clone https://github.com/yashodeepchaudhari/tenantguard.git C:\tenantguard
cd C:\tenantguard
docker compose up -d --build
docker compose ps
```
Open `http://127.0.0.1:8000/docs` (gateway) and `http://127.0.0.1:8200/docs` (brain).

## During the hackathon
- Keep the laptop **plugged in**, **Docker Desktop running**, on the same network.
- Don't close Docker Desktop or switch Wi-Fi networks. If the IP changes, tell us the new one.

## After the hackathon (security cleanup)
```powershell
Stop-Service sshd; Set-Service -Name sshd -StartupType Disabled
Remove-NetFirewallRule -DisplayName "TenantGuard"
```
Also remove the `tenantguard-coordinator` line from `administrators_authorized_keys`.

---

## Coordinator side (our laptop, for reference)
```bash
docker context create tg-host --docker "host=ssh://<user>@<ip>"   # once, uses ~/.ssh/tenantguard_ed25519 via ~/.ssh/config
docker --context tg-host compose up -d --build                    # builds from OUR files, runs on Laptop A
docker --context tg-host compose ps
```
Preferred on Windows hosts (sshd resets parallel SSH sessions): `git archive --format=tar master | ssh tg-host "tar -x -f - -C C:\Users\preeti\tenantguard-deploy"`, then run compose there over one session. Fallback:
```bash
ssh <user>@<ip> "cd C:\tenantguard && git pull && docker compose up -d --build"
```
