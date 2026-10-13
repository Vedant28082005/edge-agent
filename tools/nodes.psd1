# Target nodes. Edit User/Host if your ssh setup differs. Hosts are tailnet MagicDNS names.
@{
    brain  = @{ Host = 'pi-brain';    User = 'pi'; Dir = '~/edge-agent' }
    vision = @{ Host = 'pi-vision';   User = 'pi'; Dir = '~/edge-agent' }
    jetson = @{ Host = 'jetson-yolo'; User = 'jetson'; Dir = '~/edge-agent' }
}
