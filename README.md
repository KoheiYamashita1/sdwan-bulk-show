# sdwan-bulk-show
This sample script gets multiple show command result for multiple sdwan devices.

# how to use
Put hosts and command file in same directory.

Host file contains ipaddress(system-ip), username , passwod.

$ more hosts.txt
2.1.1.1,admin,admin
3.1.1.1,admin,admin
4.1.1.1,admin,admin

Command file conatains all you needed show commands.

$ more commands.txt
show version
show ip int bri
show ip route
show sdwan control connections

Command exapmle
#python3 multi-hosts-commands.py hosts.txt commands.txt
