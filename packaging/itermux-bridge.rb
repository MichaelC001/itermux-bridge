class ItermuxBridge < Formula
  include Language::Python::Virtualenv

  desc "Bridge iTerm2 into the real tmux protocol"
  homepage "https://github.com/wsvn53/itermux-bridge"
  url "https://github.com/wsvn53/itermux-bridge/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "7b75857e36c6003c0f204b2066dae679e9a0659f1146f943cd14fc925b7f27f3"
  license "MIT"

  depends_on "python@3.13"
  depends_on :macos
  depends_on "tmux"

  resource "iterm2" do
    url "https://files.pythonhosted.org/packages/4f/fb/258e7e3bfcacf9cdfc378ae4ee2aca743dbccd6a12ffceee12957f67dff3/iterm2-2.20.tar.gz"
    sha256 "168d3807cd58b3e678476852be2bb4a5cd89f008d95e37d2777d9810731cff08"
  end

  resource "protobuf" do
    url "https://files.pythonhosted.org/packages/66/70/e908e9c5e52ef7c3a6c7902c9dfbb34c7e29c25d2f81ade3856445fd5c94/protobuf-6.33.6.tar.gz"
    sha256 "a6768d25248312c297558af96a9f9c929e8c4cee0659cb07e780731095f38135"
  end

  resource "websockets" do
    url "https://files.pythonhosted.org/packages/21/e6/26d09fab466b7ca9c7737474c52be4f76a40301b08362eb2dbc19dcc16c1/websockets-15.0.1.tar.gz"
    sha256 "82544de02076bafba038ce055ee6412d68da13ab47f0c60cab827346de828dee"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      The bridge runs inside iTerm2 as an AutoLaunch script. To finish setup:

        itermux-bridge install

      Then enable iTerm2 -> Settings -> General -> Magic -> Python API and
      restart iTerm2. Verify with:

        itermux-bridge doctor
    EOS
  end

  test do
    # `status` needs no iTerm2 running: it reports the socket path and whether
    # anything is listening, so it exercises config loading end to end.
    assert_match "socket:", shell_output("#{bin}/itermux-bridge status")
  end
end
